import jwt
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from sqlalchemy import func
from config import settings
from database import get_db
import models


security = HTTPBearer()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

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
        user_id: str = payload.get("sub") # Sub is user ID
        if user_id is None:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
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
        user_id = payload.get("sub")
        if user_id is None:
            return None
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user:
            import backend_utils
            backend_utils.award_daily_login_credits(db, user)
        return user
    except Exception:
        return None
