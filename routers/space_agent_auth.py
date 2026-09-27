"""Browser consent for external agents; account credentials never cross the UI."""
import datetime as dt
import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field, StrictBool, StrictStr, field_validator
from sqlalchemy.orm import Session

import auth
import models
from config import settings
from database import get_db
from rate_limit import limiter, get_authenticated_or_remote_address, get_real_remote_address
from routers.space_accounts import StrictEntityModel, _api_key_response, issue_space_api_key, SPACE_API_KEY_SCOPES

router = APIRouter(prefix="/space/api/v2/agent-authorizations", tags=["space-agent-authorization"])
TTL_SECONDS = 600
POLL_SECONDS = 5
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class StartRequest(StrictEntityModel):
    name: StrictStr = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def name_required(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("A printable agent name is required")
        return value


class UserCodeRequest(StrictEntityModel):
    user_code: StrictStr = Field(min_length=8, max_length=32)


class DecisionRequest(UserCodeRequest):
    approve: StrictBool


class TokenRequest(StrictEntityModel):
    device_code: StrictStr = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


def utc(value):
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


def now():
    return dt.datetime.now(dt.timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).digest()


def result(body, status=200):
    return JSONResponse(body, status_code=status, headers=NO_STORE)


def failure(code, status=400, **extra):
    return result({"error": code, **extra}, status)


def verification_uri():
    uri = settings.SPACE_AGENT_VERIFICATION_URI
    parsed = urlsplit(uri)
    if (not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment
        or not (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
        raise HTTPException(503, detail={"code": "AGENT_AUTHORIZATION_NOT_CONFIGURED"}, headers=NO_STORE)
    return uri


def grant_for_user_code(db, code, user):
    normalized = code.upper().replace("-", "").replace(" ", "")
    grant = db.query(models.SpaceAgentAuthorization).filter_by(user_code_hash=digest(normalized)).with_for_update().first()
    if not grant or utc(grant.expires_at) <= now():
        raise HTTPException(410, detail={"code": "AGENT_AUTHORIZATION_EXPIRED"}, headers=NO_STORE)
    if grant.user_id is not None and grant.user_id != user.id:
        raise HTTPException(403, detail={"code": "AGENT_AUTHORIZATION_ACCOUNT_MISMATCH"}, headers=NO_STORE)
    return grant


@router.post("/requests", status_code=201)
@limiter.limit("5/minute; 30/hour", key_func=get_real_remote_address)
def start(request: Request, payload: StartRequest, db: Session = Depends(get_db)):
    uri = verification_uri()
    timestamp = now()
    # Indexed expiry cleanup keeps abandoned anonymous requests bounded in time.
    db.query(models.SpaceAgentAuthorization).filter(models.SpaceAgentAuthorization.expires_at <= timestamp).delete(synchronize_session=False)
    device_code = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    user_code = "-".join(code[i:i + 4] for i in range(0, 12, 4))
    db.add(models.SpaceAgentAuthorization(
        device_hash=digest(device_code), user_code_hash=digest(code), name=payload.name,
        expires_at=timestamp + dt.timedelta(seconds=TTL_SECONDS),
        next_poll_at=timestamp + dt.timedelta(seconds=POLL_SECONDS), poll_interval=POLL_SECONDS,
    ))
    db.commit()
    return result({"device_code": device_code, "user_code": user_code,
                   "verification_uri": uri, "verification_uri_complete": f"{uri}#code={user_code}",
                   "expires_in": TTL_SECONDS, "interval": POLL_SECONDS}, 201)


@router.post("/inspect")
@limiter.limit("30/minute; 300/hour", key_func=get_authenticated_or_remote_address)
def inspect(request: Request, payload: UserCodeRequest, db: Session = Depends(get_db),
            user: models.User = Depends(auth.get_current_user)):
    grant = grant_for_user_code(db, payload.user_code, user)
    body = {"name": grant.name, "status": grant.status, "expires_at": utc(grant.expires_at).isoformat(),
            "scopes": list(SPACE_API_KEY_SCOPES), "account": {"id": user.id, "email": user.email}}
    db.commit()
    return result(body)


@router.post("/decision")
@limiter.limit("10/minute; 50/hour", key_func=get_authenticated_or_remote_address)
def decide(request: Request, payload: DecisionRequest, db: Session = Depends(get_db),
           user: models.User = Depends(auth.get_current_user)):
    grant = grant_for_user_code(db, payload.user_code, user)
    desired = "approved" if payload.approve else "denied"
    if grant.status != "pending":
        if grant.status == desired or (grant.status == "claimed" and payload.approve):
            db.commit()
            return result({"status": grant.status})
        return failure("authorization_already_decided", 409)
    grant.status = desired
    grant.user_id = user.id
    db.commit()
    return result({"status": desired})


@router.post("/token")
@limiter.limit("60/minute; 1000/hour", key_func=get_real_remote_address)
def token(request: Request, payload: TokenRequest, db: Session = Depends(get_db)):
    grant = db.query(models.SpaceAgentAuthorization).filter_by(device_hash=digest(payload.device_code)).with_for_update().first()
    timestamp = now()
    if not grant:
        return failure("invalid_grant")
    if utc(grant.expires_at) <= timestamp:
        return failure("expired_token")
    if grant.status == "denied":
        return failure("access_denied")
    if utc(grant.next_poll_at) > timestamp:
        grant.poll_interval = min(60, grant.poll_interval + 5)
        grant.next_poll_at = timestamp + dt.timedelta(seconds=grant.poll_interval)
        db.commit()
        return failure("slow_down", interval=grant.poll_interval)
    grant.next_poll_at = timestamp + dt.timedelta(seconds=grant.poll_interval)
    if grant.status == "pending":
        db.commit()
        return failure("authorization_pending", interval=grant.poll_interval)

    # A domain-separated secret derived from the 256-bit private device code
    # lets response-loss retries recover the SAME key without persisting plaintext.
    secret = hashlib.sha256(b"space-agent-api-key:v1:" + payload.device_code.encode()).hexdigest()
    if grant.status == "approved":
        try:
            key, plaintext = issue_space_api_key(db, grant.user_id, grant.name, secret=secret)
        except HTTPException as error:
            db.rollback()
            return failure(error.detail["code"], error.status_code)
        grant.api_key_id = key.id
        grant.status = "claimed"
    else:
        key = db.get(models.SpaceApiKey, grant.api_key_id)
        if key is None or key.user_id != grant.user_id:
            return failure("access_denied")
        plaintext = key.key_prefix + secret
        if not hmac.compare_digest(key.token_hash, digest(plaintext)):
            return failure("invalid_grant")
    body = {**_api_key_response(key), "api_key": plaintext, "token_type": "Bearer"}
    db.commit()
    return result(body)
