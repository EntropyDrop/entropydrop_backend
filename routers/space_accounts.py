"""Account-owned API credentials stay in the cloud database."""
import datetime
import hashlib
import secrets
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict, StrictStr
from sqlalchemy.orm import Session
import auth
import models
from database import get_db
from rate_limit import limiter
api_key_router = APIRouter(prefix="/space/api/v2/api-keys", tags=["space-api-keys"])
class StrictEntityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
SPACE_ENTITY_RATE_LIMIT = "120/minute; 2000/hour"
SPACE_API_KEY_MAX_PER_USER = 20
SPACE_API_KEY_PREFIX = "edapi_"
SPACE_API_KEY_CREATE_SCOPE = "space:entity:create"
SPACE_API_KEY_RUN_SCOPE = "space:entity:run"
SPACE_API_KEY_BUILD_SCOPE = "space:blockset:build"
SPACE_API_KEY_SCOPES = (SPACE_API_KEY_CREATE_SCOPE, SPACE_API_KEY_RUN_SCOPE, SPACE_API_KEY_BUILD_SCOPE)

class CreateSpaceApiKeyRequest(StrictEntityModel):
    name: StrictStr = Field(min_length=1, max_length=80)
    scopes: list[Literal["space:entity:create", "space:entity:run", "space:blockset:build"]] = Field(
        default_factory=lambda: [SPACE_API_KEY_CREATE_SCOPE],
        min_length=1,
        max_length=len(SPACE_API_KEY_SCOPES),
    )


def _api_key_response(api_key: models.SpaceApiKey) -> dict:
    return {
        "id": api_key.id,
        "name": api_key.name,
        "key_prefix": api_key.key_prefix,
        "scopes": list(api_key.scopes or []),
        "created_at": api_key.created_at.isoformat(),
        "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
    }


@api_key_router.post("", status_code=201)
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def create_space_api_key(
    request: Request,
    payload: CreateSpaceApiKeyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail={"code": "SPACE_API_KEY_NAME_REQUIRED"})
    scopes = [scope for scope in SPACE_API_KEY_SCOPES if scope in set(payload.scopes)]
    if SPACE_API_KEY_CREATE_SCOPE not in scopes:
        raise HTTPException(status_code=422, detail={
            "code": "SPACE_API_KEY_CREATE_SCOPE_REQUIRED",
            "required_scope": SPACE_API_KEY_CREATE_SCOPE,
        })
    db.query(models.User).filter(models.User.id == current_user.id).with_for_update().first()
    count = db.query(models.SpaceApiKey).filter(
        models.SpaceApiKey.user_id == current_user.id,
    ).count()
    if count >= SPACE_API_KEY_MAX_PER_USER:
        raise HTTPException(status_code=429, detail={
            "code": "SPACE_API_KEY_LIMIT_REACHED",
            "limit": SPACE_API_KEY_MAX_PER_USER,
        })
    key_id = models.generate_base58_id()
    key_prefix = f"{SPACE_API_KEY_PREFIX}{key_id}_"
    plaintext = f"{key_prefix}{secrets.token_urlsafe(32)}"
    api_key = models.SpaceApiKey(
        id=key_id,
        user_id=current_user.id,
        name=name,
        key_prefix=key_prefix,
        token_hash=hashlib.sha256(plaintext.encode()).digest(),
        scopes=scopes,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return {**_api_key_response(api_key), "api_key": plaintext}


@api_key_router.get("")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def list_space_api_keys(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    api_keys = db.query(models.SpaceApiKey).filter(
        models.SpaceApiKey.user_id == current_user.id,
    ).order_by(models.SpaceApiKey.created_at.desc()).all()
    return {"items": [_api_key_response(api_key) for api_key in api_keys]}


@api_key_router.delete("/{api_key_id}")
@limiter.limit(SPACE_ENTITY_RATE_LIMIT)
def revoke_space_api_key(
    request: Request,
    api_key_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    api_key = db.query(models.SpaceApiKey).filter(
        models.SpaceApiKey.id == api_key_id,
        models.SpaceApiKey.user_id == current_user.id,
    ).first()
    if api_key is None:
        raise HTTPException(status_code=404, detail={"code": "SPACE_API_KEY_NOT_FOUND"})
    db.delete(api_key)
    db.commit()
    return {"revoked": True, "api_key_id": api_key_id}
