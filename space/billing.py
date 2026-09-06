"""Local grant state/outbox; cloud network calls never run in a simulation commit."""
import asyncio
import logging
import uuid
from fastapi import HTTPException
from sqlalchemy import or_, and_
from config import settings
from space import models
from space.account_client import call
from space.database import SessionLocal

log = logging.getLogger(__name__)


def ready_grant(db, entity):
    return db.query(models.SpaceHostingGrant).filter_by(world_id=entity.world_id,
        entity_id=entity.id, authorization_id=entity.hosting_authorization_id, state="ready").first()


def cancel_unused(db, entity):
    for grant in db.query(models.SpaceHostingGrant).filter(
        models.SpaceHostingGrant.world_id == entity.world_id,
        models.SpaceHostingGrant.entity_id == entity.id,
        models.SpaceHostingGrant.state.in_(["pending", "ready"])).all():
        grant.state = "cancelled"
        grant.settlement = "release"
        grant.settled = False


def authorize(db, creator, entity, payload):
    response = call("authorizations", {"credential": creator.credential,
        "operation_id": str(payload.operation_id), "world_id": entity.world_id,
        "entity_id": entity.id, "enabled": payload.enabled, "max_credits": payload.max_credits})
    if response["user_id"] != creator.user.id:
        raise HTTPException(403, detail={"code": "ENTITY_HOSTING_FORBIDDEN"})
    cancel_unused(db, entity)
    entity.hosting_authorization_id = response["id"]
    if db.get(models.SpaceHostingAuthorization, response["id"]) is None:
        db.add(models.SpaceHostingAuthorization(id=response["id"], world_id=entity.world_id,
            entity_id=entity.id, revoked=not payload.enabled))


def consume(db, entity, elapsed_ms):
    if entity.hosting_remaining_ms < elapsed_ms:
        grant = ready_grant(db, entity)
        if grant is None or entity.hosting_budget_remaining < 1:
            raise ValueError("unfunded hosted step")
        grant.state = "consumed"
        grant.settlement = "capture"
        entity.hosting_remaining_ms += 3_600_000
        entity.hosting_budget_remaining -= 1
        entity.hosting_billed_hours += 1
    entity.hosting_remaining_ms -= elapsed_ms


def reconcile(world_id):
    # Serialize grant creation/cancellation with Space mutations, then release the
    # transaction before any RPC. The simulation sees only durable ready grants.
    with SessionLocal() as db:
        world = db.query(models.SpaceWorld).filter_by(id=world_id).with_for_update().first()
        if world is None:
            return
        entities = db.query(models.SpaceWorldEntity).filter_by(world_id=world_id).all()
        by_id = {e.id: e for e in entities}
        grants = db.query(models.SpaceHostingGrant).filter(
            models.SpaceHostingGrant.world_id == world_id,
            or_(models.SpaceHostingGrant.state.in_(["pending", "ready"]),
                and_(models.SpaceHostingGrant.settlement.isnot(None), models.SpaceHostingGrant.settled.is_(False)))).all()
        authorizations = db.query(models.SpaceHostingAuthorization).filter_by(world_id=world_id, settled=False).all()
        for authorization in authorizations:
            entity = by_id.get(authorization.entity_id)
            if entity is None or not entity.hosting_enabled or entity.hosting_authorization_id != authorization.id:
                authorization.revoked = True
        for grant in grants:
            entity = by_id.get(grant.entity_id)
            if grant.state in {"pending", "ready"} and (entity is None or not entity.hosting_enabled
                    or entity.hosting_authorization_id != grant.authorization_id):
                grant.state, grant.settlement, grant.settled = "cancelled", "release", False
        for entity in entities:
            if not (entity.hosting_enabled and entity.hosting_remaining_ms == 0
                    and entity.hosting_budget_remaining > 0 and entity.hosting_authorization_id):
                continue
            if any(g.entity_id == entity.id and g.authorization_id == entity.hosting_authorization_id
                   and g.state in {"pending", "ready"} for g in grants):
                continue
            grant = models.SpaceHostingGrant(id=str(uuid.uuid4()), world_id=world_id,
                entity_id=entity.id, authorization_id=entity.hosting_authorization_id, state="pending")
            db.add(grant)
            grants.append(grant)
        db.commit()
        tasks = [(g.id, g.authorization_id, g.state, g.settlement) for g in grants
                 if g.state == "pending" or (g.settlement and not g.settled)]
        revocations = [a.id for a in authorizations if a.revoked]
    for aid in revocations:
        try:
            call("authorizations/revoke", {"id": aid})
        except HTTPException:
            continue
        with SessionLocal() as db:
            db.get(models.SpaceHostingAuthorization, aid).settled = True
            db.commit()
    for gid, aid, state, settlement in tasks:
        path = "reservations/" + settlement if settlement else "reservations"
        try:
            response = call(path, {"id": gid, "authorization_id": aid})
        except HTTPException as error:
            code = error.detail.get("code", "account_error") if isinstance(error.detail, dict) else "account_error"
            with SessionLocal() as db:
                db.query(models.SpaceWorld).filter_by(id=world_id).with_for_update().first()
                grant = db.get(models.SpaceHostingGrant, gid)
                grant.error = code
                if not settlement and error.status_code in {402, 403, 404, 409} and grant.state == "pending":
                    grant.state = "failed"
                    entity = db.get(models.SpaceWorldEntity, (world_id, grant.entity_id))
                    if entity and entity.hosting_authorization_id == aid:
                        entity.hosting_enabled = False
                        entity.desired_run_state = "stopped"
                        entity.hosting_reason = "budget_exhausted" if code == "HOSTING_BUDGET_EXHAUSTED" else "insufficient_credits" if error.status_code == 402 else code.lower()
                        entity.revision += 1
                db.commit()
            continue
        with SessionLocal() as db:
            db.query(models.SpaceWorld).filter_by(id=world_id).with_for_update().first()
            grant = db.get(models.SpaceHostingGrant, gid)
            if settlement:
                expected = "captured" if settlement == "capture" else "released"
                if response["state"] == expected and grant.settlement == settlement:
                    grant.settled, grant.error = True, None
            elif grant.state == "pending":
                if response["state"] == "reserved":
                    grant.state, grant.error = "ready", None
                else:
                    # A restored/cancelled reservation must never mint another hour.
                    grant.state, grant.error = "failed", "reservation_not_usable"
                    entity = db.get(models.SpaceWorldEntity, (world_id, grant.entity_id))
                    if entity and entity.hosting_authorization_id == aid:
                        entity.hosting_enabled = False
                        entity.desired_run_state = "stopped"
                        entity.hosting_reason = grant.error
                        entity.revision += 1
            db.commit()


async def run(world_ids):
    while True:
        if settings.SPACE_HOSTING_ENABLED:
            for world_id in world_ids:
                try:
                    await asyncio.to_thread(reconcile, world_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Space billing reconciliation failed for world %s", world_id)
        await asyncio.sleep(2)
