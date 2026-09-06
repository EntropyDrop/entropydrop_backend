"""Public agent instructions and authenticated, self-only position discovery."""
import datetime as dt
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from rate_limit import limiter
from routers import space, space_entities as entities
from space import models
from space.database import get_db

router = APIRouter(prefix="/space/api/v2", tags=["space-agent"])
public_router = APIRouter(prefix="/space/agent", tags=["space-agent-docs"])
DOCS_DIR = Path(__file__).resolve().parent.parent / "space" / "agent"
POSITION_STALE_AFTER_SECONDS = 30


class PlayerPosition(BaseModel):
    x_cm: int
    y_cm: int
    z_cm: int


class PlayerPositionResponse(BaseModel):
    world_id: str
    position: PlayerPosition
    yaw_q15: int
    pitch_q15: int
    updated_at: dt.datetime
    age_seconds: float
    stale: bool
    stale_after_seconds: int
    source: str = "checkpoint"


def _own_position(db: Session, world_id: str, creator: entities.EntityCreator):
    # The world and membership checks remain identical to external creation.
    # Never accept a caller-supplied player ID or generate a bootstrap pose.
    world = space._require_world_membership(db, world_id, creator.user)
    snapshot = db.query(models.SpacePlayerSnapshot).filter_by(
        world_id=world.id, user_id=creator.user.id,
    ).first()
    position = space._decode_player_snapshot(snapshot, world)
    if position is None or snapshot.updated_at is None:
        raise HTTPException(404, detail={
            "code": "PLAYER_POSITION_UNAVAILABLE",
            "message": "Enter this online world and wait for a position checkpoint, then retry.",
            "world_id": str(world.id),
        }, headers={"Cache-Control": "no-store"})
    updated_at = snapshot.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    delta = (now - updated_at).total_seconds()
    # Future-dated checkpoints are not trustworthy evidence of a fresh pose.
    stale = delta < -5 or delta > POSITION_STALE_AFTER_SECONDS
    result = {
        "world_id": str(world.id),
        "position": {axis: position[axis] for axis in ("x_cm", "y_cm", "z_cm")},
        "yaw_q15": position["yaw_q15"], "pitch_q15": position["pitch_q15"],
        "updated_at": updated_at, "age_seconds": round(max(0, delta), 3),
        "stale": stale, "stale_after_seconds": POSITION_STALE_AFTER_SECONDS,
        "source": "checkpoint",
    }
    # Persist API-key last_used_at and release the shared world lock promptly.
    db.commit()
    return result


@router.get("/players/me/position", response_model=PlayerPositionResponse)
@limiter.limit(entities.SPACE_ENTITY_RATE_LIMIT)
def get_my_default_world_position(request: Request, response: Response,
        db: Session = Depends(get_db),
        creator: entities.EntityCreator = Depends(entities._entity_creator)):
    """Read only the credential owner's saved position in this server's default world.

    Accepts a player login token or an existing Space API key with
    space:entity:create. Does not join a world or invent an initial position.
    """
    response.headers["Cache-Control"] = "no-store"
    return _own_position(db, settings.SPACE_DEFAULT_WORLD_ID, creator)


@router.get("/worlds/{world_id}/players/me/position", response_model=PlayerPositionResponse)
@limiter.limit(entities.SPACE_ENTITY_RATE_LIMIT)
def get_my_world_position(request: Request, response: Response, world_id: uuid.UUID,
        db: Session = Depends(get_db),
        creator: entities.EntityCreator = Depends(entities._entity_creator)):
    """Read only the credential owner's saved position in a world they have joined."""
    response.headers["Cache-Control"] = "no-store"
    return _own_position(db, str(world_id), creator)


PUBLIC_FILES = {
    "SKILL.md": ("SKILL.md", "text/markdown"),
    "spaceAPI.md": ("spaceAPI.md", "text/markdown"),
    "entityAPI.md": ("entityAPI.md", "text/markdown"),
    "references/inventory.proto": ("references/inventory.proto", "text/plain"),
    "references/entity-create.md": ("references/entity-create.md", "text/markdown"),
}


@public_router.get("/{document:path}", response_class=FileResponse)
@limiter.limit(space.SPACE_PUBLIC_STATUS_RATE_LIMIT)
def get_agent_document(request: Request, document: str):
    """Serve a fixed public documentation allowlist without account authentication."""
    if document == "references/script-api-v2.md":
        return RedirectResponse("/space/agent/entityAPI.md", status_code=308)
    entry = PUBLIC_FILES.get(document)
    if entry is None:
        raise HTTPException(404, detail={"code": "AGENT_DOCUMENT_NOT_FOUND"})
    relative, media_type = entry
    return FileResponse(DOCS_DIR / relative, media_type=media_type, headers={
        "Cache-Control": "public, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })
