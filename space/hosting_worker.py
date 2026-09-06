"""Run with python -m space.hosting_worker beside the API, never inside an HTTP request.

One fenced executor per world. Simulation runs outside database transactions; results
are committed only if the entire observed world revision still matches. Purchased
milliseconds, snapshots, terrain events and credit logs commit or roll back together.
"""
import asyncio
import base64
import datetime as dt
import json
import logging
import os
from pathlib import Path
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_

from space import models
from config import settings
from credit_balance import available_balance
from space.database import SessionLocal
from routers import space as terrain
from routers.space_entities import _decode_entity_definition, _encode_snapshot, EntityPosition, _enforce_entity_storage_quota
from routers.space_hosting import HOUR_MS, utc, validate_hosted_definition

log = logging.getLogger(__name__)
LEASE_SECONDS = 15
STEP_MS = 1000
MAX_MESSAGE = 32 * 1024 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc)


def pause(entity, reason):
    entity.hosting_enabled = False
    entity.hosting_reason = reason
    entity.desired_run_state = "stopped"
    entity.revision += 1


def charge_time(db, entity, user, elapsed_ms):
    """Caller owns world/entity/user locks; only committed runtime consumes time."""
    if not settings.SPACE_HOSTING_ENABLED:
        raise RuntimeError("Entity hosting is disabled")
    if not isinstance(elapsed_ms, int) or not 0 < elapsed_ms <= STEP_MS or elapsed_ms % 50:
        raise ValueError("invalid hosted duration")
    if settings.SPACE_STANDALONE:
        from space.billing import consume
        return consume(db, entity, elapsed_ms)
    if entity.hosting_remaining_ms < elapsed_ms:
        if entity.hosting_budget_remaining < 1 or available_balance(db, user) < 1:
            raise ValueError("unfunded hosted step")
        user.credits -= 1
        entity.hosting_budget_remaining -= 1
        entity.hosting_billed_hours += 1
        entity.hosting_remaining_ms += HOUR_MS
        db.add(models.CreditLog(user_id=user.id, amount=-1, action="space_entity_hosting",
            source=f"entity:{entity.id}:hour:{entity.hosting_billed_hours}"))
    entity.hosting_remaining_ms -= elapsed_ms


def region_chunks(entity, world, margin=3):
    x, z = entity.hosting_anchor
    cx, cz = x // 1600, z // 1600
    return {((cx + dx) % world.width_chunks, (cz + dz) % world.length_chunks)
            for dx in range(-margin, margin + 1) for dz in range(-margin, margin + 1)}


def scene_rows(db, world_id, chunks):
    filters = [and_(models.SpaceWorldEntity.position_x_cm >= cx * 1600,
                    models.SpaceWorldEntity.position_x_cm < (cx + 1) * 1600,
                    models.SpaceWorldEntity.position_z_cm >= cz * 1600,
                    models.SpaceWorldEntity.position_z_cm < (cz + 1) * 1600) for cx, cz in sorted(chunks)]
    return db.query(models.SpaceWorldEntity).filter(
        models.SpaceWorldEntity.world_id == world_id, or_(*filters)
    ).order_by(models.SpaceWorldEntity.id).limit(37).all()


def fingerprint(rows):
    return [(str(e.id), int(e.revision)) for e in rows]


def terrain_revision(db, world_id):
    stream = db.get(models.SpaceWorldEventStream, world_id)
    return int(stream.last_event_id) if stream else 0


def prepare(db, world_id, instance_id):
    if not settings.SPACE_HOSTING_ENABLED:
        return None
    world = db.query(models.SpaceWorld).filter_by(id=world_id).with_for_update().first()
    if not world or world.status != 1:
        return None
    instant = now()
    lease = db.get(models.SpaceHostingWorker, world_id)
    if lease and lease.instance_id != instance_id and utc(lease.lease_expires_at) > instant:
        return None
    if not lease:
        lease = models.SpaceHostingWorker(world_id=world_id, instance_id=instance_id, epoch=1)
        db.add(lease)
    elif lease.instance_id != instance_id or utc(lease.lease_expires_at) <= instant:
        lease.instance_id = instance_id
        lease.epoch += 1
    lease.lease_expires_at = instant + dt.timedelta(seconds=LEASE_SECONDS)
    active = db.query(models.SpaceWorldEntity).filter_by(world_id=world_id, hosting_enabled=True).order_by(models.SpaceWorldEntity.id).all()
    users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(sorted({e.owner_user_id for e in active}))).order_by(models.User.id).with_for_update().populate_existing().all()}
    funded = []
    available = {uid: available_balance(db, u) for uid, u in users.items()} if not settings.SPACE_STANDALONE else {}
    for entity in active:
        if entity.execution_lease_expires_at and utc(entity.execution_lease_expires_at) > instant:
            continue
        membership = db.get(models.SpaceWorldPlayerProfile, (world_id, entity.owner_user_id))
        if membership is None:
            pause(entity, "world_membership_required")
        elif entity.hosting_remaining_ms == 0 and entity.hosting_budget_remaining < 1:
            pause(entity, "budget_exhausted")
        elif entity.hosting_remaining_ms == 0 and not settings.SPACE_STANDALONE and available[entity.owner_user_id] < 1:
            pause(entity, "insufficient_credits")
        else:
            if settings.SPACE_STANDALONE and entity.hosting_remaining_ms == 0:
                from space.billing import ready_grant
                if ready_grant(db, entity) is None:
                    continue
            elif entity.hosting_remaining_ms == 0:
                available[entity.owner_user_id] -= 1
            funded.append(entity)
    if not funded:
        db.commit()
        return None
    chunks = set().union(*(region_chunks(e, world) for e in funded))
    rows = scene_rows(db, world_id, chunks)
    scene_valid = True
    try:
        for entity in rows:
            validate_hosted_definition(entity.definition)
    except HTTPException:
        scene_valid = False
    if len(rows) > 36 or len(funded) > 4 or not scene_valid:
        for entity in funded:
            pause(entity, "hosting_scene_limit")
        db.commit()
        return None
    by_chunk = {(r.chunk_x, r.chunk_z): r for r in db.query(models.SpaceChunkSnapshot).filter(
        models.SpaceChunkSnapshot.world_id == world_id,
        or_(*[and_(models.SpaceChunkSnapshot.chunk_x == cx, models.SpaceChunkSnapshot.chunk_z == cz) for cx, cz in sorted(chunks)])
    ).all()}
    running = {str(e.id) for e in funded}
    duration_ms = min([STEP_MS] + [int(e.hosting_remaining_ms) for e in funded if e.hosting_remaining_ms > 0])
    payload = {"world_id": world_id, "seed": world.seed, "steps": duration_ms // 50,
        "epoch": lease.epoch, "terrain_revision": terrain_revision(db, world_id),
        "scene_revision": fingerprint(rows),
        "entities": [{"id": str(e.id), "running": str(e.id) in running,
            "definition_base64": base64.b64encode(e.definition).decode(),
            "snapshot": json.loads(e.snapshot) if e.snapshot else None,
            "position": [e.position_x_cm / 100, e.position_y_cm / 100, e.position_z_cm / 100],
            "anchor": [v / 100 for v in e.hosting_anchor] if e.hosting_anchor else None,
            "yaw_quarter_turns": e.yaw_quarter_turns} for e in rows],
        "chunks": [{"chunk_x": cx, "chunk_z": cz, "revision": by_chunk[(cx, cz)].revision if (cx, cz) in by_chunk else 0,
            **terrain._decode_chunk_overlay(by_chunk.get((cx, cz)))} for cx, cz in sorted(chunks)]}
    db.commit()
    return payload


def commit_result(db, world_id, instance_id, payload, result):
    if not settings.SPACE_HOSTING_ENABLED:
        return False
    world = db.query(models.SpaceWorld).filter_by(id=world_id).with_for_update().first()
    lease = db.get(models.SpaceHostingWorker, world_id)
    if not world or world.status != 1 or not lease or lease.instance_id != instance_id or lease.epoch != payload["epoch"] or utc(lease.lease_expires_at) <= now():
        return False
    chunks = {(c["chunk_x"], c["chunk_z"]) for c in payload["chunks"]}
    rows = scene_rows(db, world_id, chunks)
    if fingerprint(rows) != payload["scene_revision"] or terrain_revision(db, world_id) != payload["terrain_revision"]:
        return False
    by_id = {str(e.id): e for e in rows}
    expected = {e["id"] for e in payload["entities"] if e["running"]}
    if result.get("error"):
        result = {"faults": [{"id": eid, "reason": "runtime_error", "message": str(result['error'])[:500]} for eid in expected]}
    if result.get("faults"):
        for fault in result["faults"]:
            if fault["id"] not in expected:
                raise ValueError("invalid fault entity")
            pause(by_id[fault["id"]], fault["reason"][:80])
            by_id[fault["id"]].hosting_error = str(fault.get("message", fault["reason"]))[:500]
        db.commit()
        return True
    if {e["id"] for e in result.get("entities", [])} != expected or len(result["entities"]) != len(expected):
        raise ValueError("invalid runtime result entities")
    owners = sorted({by_id[eid].owner_user_id for eid in expected})
    users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(owners)).order_by(models.User.id).with_for_update().populate_existing().all()}
    # Re-check balances under lock: another world/generation may have spent credits meanwhile.
    available = {uid: available_balance(db, u) for uid, u in users.items()} if not settings.SPACE_STANDALONE else {}
    for eid in sorted(expected):
        entity = by_id[eid]
        if not entity.hosting_enabled:
            return False
        if db.get(models.SpaceWorldPlayerProfile, (world_id, entity.owner_user_id)) is None:
            pause(entity, "world_membership_required")
            db.commit()
            return False
        if entity.hosting_remaining_ms < payload["steps"] * 50:
            if settings.SPACE_STANDALONE:
                from space.billing import ready_grant
                if entity.hosting_budget_remaining < 1 or ready_grant(db, entity) is None:
                    return False
            elif entity.hosting_budget_remaining < 1 or available[entity.owner_user_id] < 1:
                pause(entity, "insufficient_credits" if available[entity.owner_user_id] < 1 else "budget_exhausted")
                db.commit()
                return False
            if not settings.SPACE_STANDALONE:
                available[entity.owner_user_id] -= 1
    mutations = result.get("mutations", [])
    if len(mutations) > 256:
        raise ValueError("hosting mutation limit")
    for item in result["entities"]:
        entity = by_id[item["id"]]
        if not isinstance(item["elapsed_ms"], int) or not 0 < item["elapsed_ms"] <= payload["steps"] * 50 or item["elapsed_ms"] % 50:
            raise ValueError("invalid elapsed time")
        definition, digest, _ = _decode_entity_definition(item["definition_base64"])
        validate_hosted_definition(definition)
        snapshot = item["snapshot"]
        x, y, z = snapshot["position"]
        position = EntityPosition(x_cm=round(x * 100) % (world.width_chunks * 1600),
                                  y_cm=round(y * 100), z_cm=round(z * 100) % (world.length_chunks * 1600))
        encoded, snapshot_digest = _encode_snapshot(snapshot, world, position)
        _enforce_entity_storage_quota(db, world, users[entity.owner_user_id],
            incoming_bytes=len(definition) + len(encoded), replaced_bytes=entity.size_bytes + entity.snapshot_size_bytes)
        entity.definition, entity.content_digest, entity.size_bytes = definition, digest, len(definition)
        entity.snapshot, entity.snapshot_digest, entity.snapshot_size_bytes = encoded, snapshot_digest, len(encoded)
        entity.position_x_cm, entity.position_y_cm, entity.position_z_cm = position.x_cm, position.y_cm, position.z_cm
        entity.hosting_last_tick_at = now()
        entity.hosting_reason = None
        entity.hosting_error = None
        entity.revision += 1
        entity.updated_at = now()
        charge_time(db, entity, users[entity.owner_user_id], item["elapsed_ms"])
        if item.get("stopped"):
            pause(entity, "script_stopped")
        elif entity.hosting_remaining_ms == 0 and entity.hosting_budget_remaining == 0:
            pause(entity, "budget_exhausted")
        db.flush()  # the next entity's aggregate storage check includes this update
    grouped = {}
    for raw_mutation in mutations:
        mutation = dict(raw_mutation)
        eid = mutation.pop("actor_entity_id")
        if eid not in expected:
            raise ValueError("invalid mutation actor")
        # Commands may only touch the originating entity's reserved neighbourhood.
        allowed = region_chunks(by_id[eid], world)
        x = mutation.get("x", mutation.get("mx", 0) // 5)
        z = mutation.get("z", mutation.get("mz", 0) // 5)
        if (x // 16, z // 16) not in allowed:
            raise ValueError("hosting mutation area limit")
        grouped.setdefault(by_id[eid].owner_user_id, []).append(mutation)
    db.flush()
    for owner_id, edits in sorted(grouped.items()):
        terrain._apply_terrain_mutation_batch(None, uuid.UUID(world_id),
            terrain.TerrainMutationBatchRequest(batch_id=uuid.uuid4(), mutations=edits), db, users[owner_id],
            hosted_chunks=chunks, commit=False)
    lease.lease_expires_at = now() + dt.timedelta(seconds=LEASE_SECONDS)
    db.commit()
    if mutations:
        from routers.space_realtime import realtime_hub
        realtime_hub.notify_terrain_from_thread(world_id, terrain_revision(db, world_id))
    return True


class NodeRuntime:
    def __init__(self):
        self.process = None

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        self.process = None

    async def step(self, payload):
        if not self.process or self.process.returncode is not None:
            entry = os.getenv("SPACE_HOSTING_RUNTIME_PATH", str(Path(__file__).resolve().parent /
                "runtime/dist/hosting-runtime.mjs"))
            if not Path(entry).is_file():
                raise RuntimeError("hosting runtime is not built; run npm ci and npm run build in space/runtime")
            self.process = await asyncio.create_subprocess_exec(os.getenv("SPACE_HOSTING_NODE", "node"),
                "--max-old-space-size=256", entry, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, limit=MAX_MESSAGE,
                # Do not forward database/payment/cloud credentials to the simulation process.
                env={k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "TMPDIR"}})
        message = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        if len(message) > MAX_MESSAGE:
            raise ValueError("hosting input too large")
        self.process.stdin.write(message)
        await self.process.stdin.drain()
        response = await self.process.stdout.readline()
        if not response:
            raise RuntimeError("hosting runtime exited")
        return json.loads(response)


async def run_world(world_id):
    instance_id = str(uuid.uuid4())
    runtime = NodeRuntime()
    try:
        while True:
            started = time.monotonic()
            payload = None
            try:
                if runtime.process is None or runtime.process.returncode is not None:
                    probe = await asyncio.wait_for(runtime.step({"probe": True}), timeout=10)
                    if not probe.get("ready"):
                        raise RuntimeError("Hosting runtime probe failed")
                with SessionLocal() as db:
                    payload = prepare(db, world_id, instance_id)
                if payload:
                    result = await asyncio.wait_for(runtime.step(payload), timeout=10)
                    with SessionLocal() as db:
                        commit_result(db, world_id, instance_id, payload, result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await runtime.close()
                log.exception("Hosted world %s failed", world_id)
                if payload:
                    code = error.detail.get("code", "runtime_error") if isinstance(error, HTTPException) and isinstance(error.detail, dict) else "runtime_error"
                    try:
                        with SessionLocal() as db:
                            commit_result(db, world_id, instance_id, payload, {"faults": [
                                {"id": e["id"], "reason": code} for e in payload["entities"] if e["running"]]})
                    except Exception:
                        log.exception("Could not persist hosted failure; retrying world %s", world_id)
            await asyncio.sleep(max(0.05, (payload["steps"] * 0.05 if payload else 1) - (time.monotonic() - started)))
    finally:
        await runtime.close()


async def main():
    if not settings.SPACE_HOSTING_ENABLED:
        raise RuntimeError("Entity hosting is disabled; enable SPACE_HOSTING_ENABLED only after deployment validation")
    # One world by default; explicit IDs keep the process resource budget predictable.
    requested = os.getenv("SPACE_HOSTING_WORLD_IDS", settings.SPACE_DEFAULT_WORLD_ID).split(",")
    with SessionLocal() as db:
        ids = [str(w.id) for w in db.query(models.SpaceWorld).filter(
            models.SpaceWorld.status == 1, models.SpaceWorld.id.in_([str(uuid.UUID(s.strip())) for s in requested])
        ).limit(4).all()]
    if not ids:
        raise RuntimeError("Bootstrap a Space world before starting the hosting worker")
    jobs = [run_world(world_id) for world_id in ids]
    if settings.SPACE_STANDALONE:
        from space.billing import run as run_billing
        jobs.append(run_billing(ids))
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
