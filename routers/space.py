import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import models
from config import settings
from database import get_db


router = APIRouter(prefix="/space/api/v2", tags=["space"])


class SpaceWorldResponse(BaseModel):
    id: str
    name: str
    seed: int
    terrain_generator_version: int


class SpacePlayerResponse(BaseModel):
    user_id: str
    username: str | None
    player_entity_id: str
    minecraft_skin_url: str
    minecraft_skin_model: str
    spawn_x_cm: int
    spawn_y_cm: int
    spawn_z_cm: int
    spawn_yaw_q15: int


class SpaceBootstrapResponse(BaseModel):
    protocol_version: int
    max_online_players: int
    queue_enabled: bool
    websocket_url: str
    world: SpaceWorldResponse
    player: SpacePlayerResponse


def _get_or_create_default_world(db: Session) -> models.SpaceWorld:
    world = db.query(models.SpaceWorld).filter(
        models.SpaceWorld.id == settings.SPACE_DEFAULT_WORLD_ID
    ).first()
    if world:
        return world

    world = models.SpaceWorld(
        id=settings.SPACE_DEFAULT_WORLD_ID,
        owner_user_id=None,
        name="EntropyDrop Space",
        seed=settings.SPACE_WORLD_SEED,
        max_online_players=32,
    )
    db.add(world)
    try:
        db.commit()
        db.refresh(world)
        return world
    except IntegrityError:
        # Another API replica may have created the singleton concurrently.
        db.rollback()
        existing = db.query(models.SpaceWorld).filter(
            models.SpaceWorld.id == settings.SPACE_DEFAULT_WORLD_ID
        ).first()
        if existing is None:
            raise
        return existing


def _random_birth_point(world: models.SpaceWorld) -> tuple[int, int, int, int]:
    # Keep the birth point one zone away from the coordinate boundary. The
    # authoritative world generator validates/refines safe ground when the
    # multiplayer worker is introduced; 32 m starts above current terrain.
    margin_cm = min(32 * 16 * 100, (world.width_chunks * 16 * 100) // 4)
    width_cm = world.width_chunks * 16 * 100
    length_cm = world.length_chunks * 16 * 100
    x = margin_cm + secrets.randbelow(max(1, width_cm - margin_cm * 2))
    z_margin = min(margin_cm, length_cm // 4)
    z = z_margin + secrets.randbelow(max(1, length_cm - z_margin * 2))
    yaw = secrets.randbelow(65535) - 32767
    return x, 3200, z, yaw


def _get_or_create_player_profile(
    db: Session,
    world: models.SpaceWorld,
    user: models.User,
) -> models.SpaceWorldPlayerProfile:
    profile = db.query(models.SpaceWorldPlayerProfile).filter(
        models.SpaceWorldPlayerProfile.world_id == world.id,
        models.SpaceWorldPlayerProfile.user_id == user.id,
    ).first()
    if profile:
        return profile

    x, y, z, yaw = _random_birth_point(world)
    profile = models.SpaceWorldPlayerProfile(
        world_id=world.id,
        user_id=user.id,
        player_entity_id=str(uuid.uuid4()),
        spawn_x_cm=x,
        spawn_y_cm=y,
        spawn_z_cm=z,
        spawn_yaw_q15=yaw,
    )
    db.add(profile)
    try:
        db.commit()
        db.refresh(profile)
        return profile
    except IntegrityError:
        # The composite PK makes concurrent first entries choose one durable row.
        db.rollback()
        existing = db.query(models.SpaceWorldPlayerProfile).filter(
            models.SpaceWorldPlayerProfile.world_id == world.id,
            models.SpaceWorldPlayerProfile.user_id == user.id,
        ).first()
        if existing is None:
            raise
        return existing


@router.post("/bootstrap", response_model=SpaceBootstrapResponse)
def bootstrap_space(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Gate Space entry and return this EntropyDrop user's stable birth point."""
    skin_url = (current_user.minecraft_skin_url or "").strip()
    if not skin_url:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SKIN_REQUIRED",
                "message": "进入 Space 前需要先设置角色皮肤。",
                "action_url": "/skin/edit",
            },
        )

    world = _get_or_create_default_world(db)
    profile = _get_or_create_player_profile(db, world, current_user)
    skin_model = "slim" if (current_user.minecraft_skin_model or "").lower() == "slim" else "strong"

    return {
        "protocol_version": 2,
        "max_online_players": min(32, world.max_online_players),
        "queue_enabled": True,
        "websocket_url": settings.SPACE_WS_URL,
        "world": {
            "id": str(world.id),
            "name": world.name,
            "seed": world.seed,
            "terrain_generator_version": world.terrain_generator_version,
        },
        "player": {
            "user_id": current_user.id,
            "username": current_user.username,
            "player_entity_id": str(profile.player_entity_id),
            "minecraft_skin_url": skin_url,
            "minecraft_skin_model": skin_model,
            "spawn_x_cm": profile.spawn_x_cm,
            "spawn_y_cm": profile.spawn_y_cm,
            "spawn_z_cm": profile.spawn_z_cm,
            "spawn_yaw_q15": profile.spawn_yaw_q15,
        },
    }
