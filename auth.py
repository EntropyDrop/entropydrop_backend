import jwt
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import HTTPException, Security, Depends, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from sqlalchemy import func
from config import settings
from database import get_db
import models


security = HTTPBearer()

ACCESS_TOKEN_EXPIRE_SECONDS = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _session_token_hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _parse_session_cookie(raw_token: str | None) -> tuple[str, str] | None:
    if not raw_token or raw_token.count(".") != 1:
        return None
    session_id, secret = raw_token.split(".", 1)
    if not (16 <= len(session_id) <= 32 and 32 <= len(secret) <= 64):
        return None
    return session_id, secret


def create_access_token(data: dict, session_id: str | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({
        "exp": expire,
        "type": "access",
        "jti": secrets.token_urlsafe(18),
    })
    if session_id:
        to_encode["sid"] = session_id
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def create_auth_session(db: Session, user_id: str) -> tuple[models.AuthSession, str]:
    now = datetime.now(timezone.utc)
    idle_expires_at = now + timedelta(days=settings.AUTH_SESSION_IDLE_DAYS)
    absolute_expires_at = now + timedelta(days=settings.AUTH_SESSION_ABSOLUTE_DAYS)
    session_id = secrets.token_urlsafe(24)[:32]
    secret = secrets.token_urlsafe(32)

    active_sessions = db.query(models.AuthSession).filter(
        models.AuthSession.user_id == user_id,
        models.AuthSession.revoked_at.is_(None),
        models.AuthSession.expires_at > now,
        models.AuthSession.absolute_expires_at > now,
    ).order_by(models.AuthSession.last_used_at.desc()).all()
    keep_existing = max(0, settings.AUTH_SESSION_MAX_PER_USER - 1)
    for stale_session in active_sessions[keep_existing:]:
        stale_session.revoked_at = now

    session = models.AuthSession(
        id=session_id,
        user_id=user_id,
        token_hash=_session_token_hash(secret),
        created_at=now,
        last_used_at=now,
        expires_at=idle_expires_at,
        absolute_expires_at=absolute_expires_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, f"{session_id}.{secret}"


def refresh_auth_session(db: Session, raw_token: str | None) -> models.AuthSession:
    parsed = _parse_session_cookie(raw_token)
    if parsed is None:
        raise HTTPException(status_code=401, detail={"code": "SESSION_REQUIRED"})
    session_id, secret = parsed
    session = db.query(models.AuthSession).filter(
        models.AuthSession.id == session_id,
    ).with_for_update().first()
    supplied_hash = _session_token_hash(secret)
    if session is None or not hmac.compare_digest(bytes(session.token_hash), supplied_hash):
        raise HTTPException(status_code=401, detail={"code": "SESSION_INVALID"})

    now = datetime.now(timezone.utc)
    if (
        session.revoked_at is not None
        or _utc(session.expires_at) <= now
        or _utc(session.absolute_expires_at) <= now
    ):
        if session.revoked_at is None:
            session.revoked_at = now
            db.commit()
        raise HTTPException(status_code=401, detail={"code": "SESSION_EXPIRED"})

    session.last_used_at = now
    session.expires_at = min(
        now + timedelta(days=settings.AUTH_SESSION_IDLE_DAYS),
        _utc(session.absolute_expires_at),
    )
    db.commit()
    db.refresh(session)
    return session


def revoke_auth_session(db: Session, raw_token: str | None) -> None:
    parsed = _parse_session_cookie(raw_token)
    if parsed is None:
        return
    session_id, secret = parsed
    session = db.query(models.AuthSession).filter(models.AuthSession.id == session_id).first()
    if session is None or not hmac.compare_digest(
        bytes(session.token_hash),
        _session_token_hash(secret),
    ):
        return
    if session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        db.commit()


def set_auth_session_cookie(response: Response, raw_token: str, expires_at: datetime) -> None:
    expires_at = _utc(expires_at)
    max_age = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
    response.set_cookie(
        key=settings.AUTH_SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        expires=expires_at,
        path="/skin/api/auth",
        domain=settings.AUTH_SESSION_COOKIE_DOMAIN or None,
        secure=settings.AUTH_SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def clear_auth_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.AUTH_SESSION_COOKIE_NAME,
        path="/skin/api/auth",
        domain=settings.AUTH_SESSION_COOKIE_DOMAIN or None,
        secure=settings.AUTH_SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def _user_from_access_payload(db: Session, payload: dict) -> models.User | None:
    user_id = payload.get("sub")
    if not user_id:
        return None
    query = db.query(models.User).filter(models.User.id == user_id)
    session_id = payload.get("sid")
    if session_id:
        now = datetime.now(timezone.utc)
        query = query.join(
            models.AuthSession,
            models.AuthSession.user_id == models.User.id,
        ).filter(
            models.AuthSession.id == session_id,
            models.AuthSession.revoked_at.is_(None),
            models.AuthSession.expires_at > now,
            models.AuthSession.absolute_expires_at > now,
        )
    return query.first()


def verify_google_token(token: str):
    try:
        # Verify Google JWT token from frontend
        # GOOGLE_CLIENT_ID must be configured to validate audience
        if not settings.GOOGLE_CLIENT_ID:
            raise HTTPException(status_code=500, detail="Google authentication is not configured")
        
        # requests instance is used to fetch public keys from Google
        request = google_requests.Request()
        
        id_info = id_token.verify_oauth2_token(token, request, audience=settings.GOOGLE_CLIENT_ID)
        return id_info
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Google token")

def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security), db: Session = Depends(get_db)):
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") not in (None, "access"):
            raise HTTPException(status_code=401, detail="Could not validate credentials")
        user = _user_from_access_payload(db, payload)
        if user is None:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    
    import backend_utils
    backend_utils.award_daily_login_credits(db, user)
    return user

def get_current_admin(user: models.User = Depends(get_current_user)):
    admin_emails = [e.strip().lower() for e in settings.ADMIN_EMAILS.split(",") if e.strip()]
    if not user.email or user.email.lower() not in admin_emails:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

def get_current_user_optional(credentials: Optional[HTTPAuthorizationCredentials] = Security(HTTPBearer(auto_error=False)), db: Session = Depends(get_db)):
    if not credentials:
        return None
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("type") not in (None, "access"):
            return None
        user = _user_from_access_payload(db, payload)
        if user:
            import backend_utils
            backend_utils.award_daily_login_credits(db, user)
        return user
    except Exception:
        return None
