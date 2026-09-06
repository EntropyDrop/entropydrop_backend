"""Cloud account boundary for independent Space services.

Holds reduce spendable balance, not the ledger balance. Only a captured hold creates
a debit. All transitions lock the user before the authorization/reservation; release
tombstones make a delayed reserve harmless after a cancelled local operation.
"""
import datetime as dt
import hashlib
import hmac
import json
import time
import uuid
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.orm import Session
import auth
import models
from config import settings
from credit_balance import available_balance, lock_balance
from database import get_db
from rate_limit import limiter
from routers.space_accounts import SPACE_API_KEY_SCOPES


def require_service(x_space_service_token: str = Header(default="")):
    if not settings.SPACE_ACCOUNT_SERVICE_TOKEN:
        raise HTTPException(503, detail={"code": "SPACE_ACCOUNT_SERVICE_DISABLED"})
    if not hmac.compare_digest(x_space_service_token, settings.SPACE_ACCOUNT_SERVICE_TOKEN):
        raise HTTPException(401, detail={"code": "SPACE_SERVICE_UNAUTHORIZED"})


router = APIRouter(prefix="/internal/space", dependencies=[Depends(require_service)], tags=["space-accounts"])


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=1, max_length=8192)


def credential_identity(db, credential):
    expires = time.time() + 30
    scopes = None
    if credential.startswith("edapi_"):
        key = db.query(models.SpaceApiKey).filter_by(token_hash=hashlib.sha256(credential.encode()).digest()).first()
        if key is None:
            raise HTTPException(401, detail={"code": "SPACE_API_KEY_INVALID"})
        user = db.get(models.User, key.user_id)
        scopes = list(SPACE_API_KEY_SCOPES)
        key.last_used_at = dt.datetime.now(dt.timezone.utc)
    else:
        user = auth.get_current_user(credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=credential), db=db)
        payload = jwt.decode(credential, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        expires = min(expires, float(payload["exp"]))
    if user is None:
        raise HTTPException(401, detail={"code": "ACCOUNT_NOT_FOUND"})
    return user, scopes, expires


@router.post("/identity")
@limiter.exempt
def identity(payload: Credential, db: Session = Depends(get_db)):
    user, scopes, expires = credential_identity(db, payload.credential)
    result = {"id": user.id, "username": user.username, "skin_url": user.skin_url,
              "skin_type": user.skin_type, "is_admin": user.is_admin and scopes is None,
              "credits": available_balance(db, user), "scopes": scopes, "expires_at": expires,
              "api_key_count": db.query(models.SpaceApiKey).filter_by(user_id=user.id).count()}
    db.commit()
    return result


class AuthorizationRequest(Credential):
    operation_id: uuid.UUID
    world_id: uuid.UUID
    entity_id: uuid.UUID
    enabled: StrictBool
    max_credits: StrictInt = Field(ge=0, le=168)


@router.post("/authorizations")
@limiter.exempt
def authorize(payload: AuthorizationRequest, db: Session = Depends(get_db)):
    user, _, _ = credential_identity(db, payload.credential)
    lock_balance(db, user)
    aid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"space:{payload.world_id}:{payload.operation_id}"))
    body = payload.model_dump(mode="json", exclude={"credential"})
    digest = hashlib.sha256(json.dumps({**body, "user_id": user.id}, sort_keys=True).encode()).digest()
    previous = db.get(models.SpaceCreditAuthorization, aid)
    if previous:
        if bytes(previous.request_digest) != digest:
            raise HTTPException(409, detail={"code": "ENTITY_OPERATION_ID_REUSED"})
        db.commit()
        return {"id": aid, "user_id": user.id}
    old = db.query(models.SpaceCreditAuthorization).filter_by(world_id=str(payload.world_id), entity_id=str(payload.entity_id)).all()
    if any(row.user_id != user.id for row in old):
        raise HTTPException(403, detail={"code": "ENTITY_HOSTING_FORBIDDEN"})
    for row in old:
        row.enabled = False
    db.add(models.SpaceCreditAuthorization(id=aid, user_id=user.id, world_id=str(payload.world_id),
        entity_id=str(payload.entity_id), request_digest=digest, enabled=payload.enabled,
        max_credits=payload.max_credits if payload.enabled else 0))
    db.commit()
    return {"id": aid, "user_id": user.id}


class ReservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    authorization_id: uuid.UUID


def locked_reservation(db, payload):
    authorization = db.get(models.SpaceCreditAuthorization, str(payload.authorization_id))
    if authorization is None:
        raise HTTPException(404, detail={"code": "HOSTING_AUTHORIZATION_NOT_FOUND"})
    user = db.get(models.User, authorization.user_id)
    lock_balance(db, user)
    db.refresh(authorization, with_for_update=True)
    reservation = db.query(models.SpaceCreditReservation).filter_by(id=str(payload.id)).with_for_update().first()
    if reservation and reservation.authorization_id != authorization.id:
        raise HTTPException(409, detail={"code": "HOSTING_RESERVATION_ID_REUSED"})
    return authorization, user, reservation


@router.post("/reservations")
@limiter.exempt
def reserve(payload: ReservationRequest, db: Session = Depends(get_db)):
    authorization, user, reservation = locked_reservation(db, payload)
    if reservation is None:
        if not authorization.enabled:
            raise HTTPException(409, detail={"code": "HOSTING_AUTHORIZATION_REVOKED"})
        spent = db.query(models.SpaceCreditReservation).filter(
            models.SpaceCreditReservation.authorization_id == authorization.id,
            models.SpaceCreditReservation.state.in_(["reserved", "captured"])).count()
        if spent >= authorization.max_credits:
            raise HTTPException(402, detail={"code": "HOSTING_BUDGET_EXHAUSTED"})
        if available_balance(db, user) < 1:
            raise HTTPException(402, detail={"code": "HOSTING_CREDITS_REQUIRED"})
        reservation = models.SpaceCreditReservation(id=str(payload.id), authorization_id=authorization.id,
            user_id=user.id, state="reserved")
        db.add(reservation)
    result = {"id": reservation.id, "state": reservation.state, "milliseconds": 3_600_000}
    db.commit()
    return result


@router.post("/reservations/{operation}")
@limiter.exempt
def settle(operation: str, payload: ReservationRequest, db: Session = Depends(get_db)):
    if operation not in {"capture", "release"}:
        raise HTTPException(404)
    authorization, user, reservation = locked_reservation(db, payload)
    if reservation is None:
        if operation != "release":
            raise HTTPException(409, detail={"code": "HOSTING_RESERVATION_NOT_READY"})
        reservation = models.SpaceCreditReservation(id=str(payload.id), authorization_id=authorization.id,
            user_id=user.id, state="released")
        db.add(reservation)
    target = "captured" if operation == "capture" else "released"
    if reservation.state not in {"reserved", target}:
        raise HTTPException(409, detail={"code": "HOSTING_SETTLEMENT_CONFLICT"})
    if reservation.state == "reserved":
        if operation == "capture":
            if user.credits < 1:
                raise HTTPException(409, detail={"code": "HOSTING_RESERVATION_BALANCE_MISMATCH"})
            user.credits -= 1
            db.add(models.CreditLog(user_id=user.id, amount=-1, action="space_entity_hosting",
                source=f"space-grant:{reservation.id}"))
        reservation.state = target
        reservation.updated_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return {"id": reservation.id, "state": reservation.state}


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID


@router.post("/authorizations/revoke")
@limiter.exempt
def revoke(payload: RevokeRequest, db: Session = Depends(get_db)):
    authorization = db.get(models.SpaceCreditAuthorization, str(payload.id))
    if authorization is None:
        raise HTTPException(404, detail={"code": "HOSTING_AUTHORIZATION_NOT_FOUND"})
    lock_balance(db, db.get(models.User, authorization.user_id))
    db.refresh(authorization, with_for_update=True)
    authorization.enabled = False
    db.commit()
    return {"id": authorization.id, "revoked": True}
