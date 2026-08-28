import asyncio
import datetime
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

import jwt
import msgpack
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import auth
import models
from config import settings
from database import SessionLocal, get_db
from rate_limit import limiter
from routers import space as space_api


logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/space/api/v2", tags=["space"])
realtime_router = APIRouter(tags=["space"])

SPACE_REALTIME_PROTOCOL = "space-relay-v1"
SPACE_JOIN_TICKET_TTL_SECONDS = 30
SPACE_REALTIME_MAX_MESSAGE_BYTES = 4096
SPACE_REALTIME_IDLE_TIMEOUT_SECONDS = 30
SPACE_REALTIME_MAX_BUFFERED_SEND_SECONDS = 0.05

_ticket_redis = Redis.from_url(
    settings.REDIS_URL,
    health_check_interval=20,
    socket_timeout=2,
    socket_connect_timeout=2,
    retry_on_timeout=True,
)
_used_ticket_lock = threading.Lock()
_used_ticket_jtis: dict[str, float] = {}


class SpaceJoinTicketResponse(BaseModel):
    ticket: str
    websocket_url: str
    expires_in_seconds: int


@dataclass
class RealtimeIdentity:
    world_id: str
    user_id: str
    username: str
    player_entity_id: str
    minecraft_skin_url: str
    minecraft_skin_model: str
    world_width_cm: int
    world_length_cm: int


@dataclass
class RealtimeSession:
    websocket: WebSocket
    identity: RealtimeIdentity
    pose: dict[str, int] | None = None
    pose_updated_at: str | None = None
    last_sequence: int = -1
    last_packet_at: float = field(default_factory=time.monotonic)
    rate_window_started_at: float = field(default_factory=time.monotonic)
    rate_window_packets: int = 0
    dirty: bool = False
    connection_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))


def _is_production() -> bool:
    return settings.ENVIRONMENT.strip().lower() in {"prod", "production"}


def _allowed_origins() -> set[str]:
    configured = {
        origin.strip()
        for origin in settings.SPACE_WS_ALLOWED_ORIGINS.split(",")
        if origin.strip()
    }
    if configured:
        return configured
    return {
        "https://entropydrop.com",
        "https://www.entropydrop.com",
        "http://localhost:5173",
        "http://localhost:3000",
    }


def _create_join_ticket(world_id: str, user_id: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "world_id": world_id,
            "type": "space-realtime",
            "jti": secrets.token_urlsafe(18),
            "iat": now,
            "exp": now + datetime.timedelta(seconds=SPACE_JOIN_TICKET_TTL_SECONDS),
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _decode_join_ticket(ticket: str) -> dict:
    try:
        payload = jwt.decode(
            ticket,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail={"code": "SPACE_JOIN_TICKET_EXPIRED"}) from exc
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail={"code": "SPACE_JOIN_TICKET_INVALID"}) from exc
    if (
        payload.get("type") != "space-realtime"
        or not payload.get("sub")
        or not payload.get("world_id")
        or not payload.get("jti")
    ):
        raise HTTPException(status_code=401, detail={"code": "SPACE_JOIN_TICKET_INVALID"})
    return payload


def _consume_ticket_once(jti: str) -> bool:
    key = f"space:realtime:ticket-used:{jti}"
    try:
        return bool(_ticket_redis.set(key, b"1", ex=SPACE_JOIN_TICKET_TTL_SECONDS * 2, nx=True))
    except Exception:
        if _is_production():
            logger.exception("Redis is required to consume Space realtime tickets in production")
            return False
        now = time.monotonic()
        with _used_ticket_lock:
            expired = [key for key, expires_at in _used_ticket_jtis.items() if expires_at <= now]
            for expired_key in expired:
                _used_ticket_jtis.pop(expired_key, None)
            if jti in _used_ticket_jtis:
                return False
            _used_ticket_jtis[jti] = now + SPACE_JOIN_TICKET_TTL_SECONDS * 2
            return True


def _authenticate_realtime_ticket(ticket: str) -> RealtimeIdentity:
    payload = _decode_join_ticket(ticket)
    if not _consume_ticket_once(str(payload["jti"])):
        raise HTTPException(status_code=401, detail={"code": "SPACE_JOIN_TICKET_ALREADY_USED"})

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == payload["sub"]).first()
        world = db.query(models.SpaceWorld).filter(models.SpaceWorld.id == payload["world_id"]).first()
        profile = db.query(models.SpaceWorldPlayerProfile).filter(
            models.SpaceWorldPlayerProfile.world_id == payload["world_id"],
            models.SpaceWorldPlayerProfile.user_id == payload["sub"],
        ).first()
        if user is None or world is None or profile is None:
            raise HTTPException(status_code=403, detail={"code": "WORLD_MEMBERSHIP_REQUIRED"})
        skin_url = (user.minecraft_skin_url or "").strip()
        if not skin_url:
            raise HTTPException(status_code=403, detail={"code": "SKIN_REQUIRED"})
        return RealtimeIdentity(
            world_id=str(world.id),
            user_id=user.id,
            username=user.username or f"Player-{user.id[:6]}",
            player_entity_id=str(profile.player_entity_id),
            minecraft_skin_url=skin_url,
            minecraft_skin_model="slim" if (user.minecraft_skin_model or "").lower() == "slim" else "strong",
            world_width_cm=world.width_chunks * space_api.SPACE_CHUNK_SIZE * 100,
            world_length_cm=world.length_chunks * space_api.SPACE_CHUNK_SIZE * 100,
        )
    finally:
        db.close()


def _persist_realtime_poses(
    records: list[tuple[RealtimeIdentity, dict[str, int]]],
    retry_insert_race: bool = True,
) -> None:
    if not records:
        return
    db = SessionLocal()
    try:
        for identity, pose in records:
            snapshot = db.query(models.SpacePlayerSnapshot).filter(
                models.SpacePlayerSnapshot.world_id == identity.world_id,
                models.SpacePlayerSnapshot.user_id == identity.user_id,
            ).with_for_update().first()
            encoded = space_api._encode_player_snapshot(pose)
            now = datetime.datetime.now(datetime.timezone.utc)
            if snapshot is None:
                snapshot = models.SpacePlayerSnapshot(
                    world_id=identity.world_id,
                    user_id=identity.user_id,
                    revision=1,
                    last_event_id=0,
                    state_version=1,
                    state=encoded,
                    updated_at=now,
                )
                db.add(snapshot)
            else:
                snapshot.revision = int(snapshot.revision or 0) + 1
                snapshot.state_version = 1
                snapshot.state = encoded
                snapshot.updated_at = now
        db.commit()
    except IntegrityError:
        db.rollback()
        if retry_insert_race:
            db.close()
            _persist_realtime_poses(records, retry_insert_race=False)
            return
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


class SpaceRealtimeHub:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, RealtimeSession]] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.fanout_tasks: dict[str, asyncio.Task] = {}
        self.remote_states: dict[str, dict[str, dict]] = {}
        self.pending_pose_events: dict[str, dict[str, dict]] = {}
        self.pending_control_events: dict[str, list[dict]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.terrain_revisions: dict[str, int] = {}
        self.sent_terrain_revisions: dict[str, int] = {}
        self.last_redis_warning_at = 0.0
        self.redis = AsyncRedis.from_url(
            settings.REDIS_URL,
            health_check_interval=20,
            socket_timeout=2,
            socket_connect_timeout=2,
            retry_on_timeout=True,
        )

    async def register(self, session: RealtimeSession) -> None:
        self.loop = asyncio.get_running_loop()
        world_sessions = self.sessions.setdefault(session.identity.world_id, {})
        previous = world_sessions.get(session.identity.user_id)
        world_sessions[session.identity.user_id] = session
        if previous is not None and previous.websocket is not session.websocket:
            try:
                await previous.websocket.close(code=4409, reason="A newer Space session replaced this connection")
            except Exception:
                pass
        task = self.tasks.get(session.identity.world_id)
        if task is None or task.done():
            self.tasks[session.identity.world_id] = asyncio.create_task(
                self._world_loop(session.identity.world_id)
            )
        fanout_task = self.fanout_tasks.get(session.identity.world_id)
        if (
            settings.SPACE_REALTIME_REDIS_FANOUT_ENABLED
            and (fanout_task is None or fanout_task.done())
        ):
            self.fanout_tasks[session.identity.world_id] = asyncio.create_task(
                self._fanout_loop(session.identity.world_id)
            )

    async def unregister(self, session: RealtimeSession) -> None:
        world_sessions = self.sessions.get(session.identity.world_id)
        if world_sessions and world_sessions.get(session.identity.user_id) is session:
            world_sessions.pop(session.identity.user_id, None)
            if not world_sessions:
                self.sessions.pop(session.identity.world_id, None)
        if session.pose is not None:
            await self._publish_event(session.identity.world_id, {
                "type": "leave",
                "user_id": session.identity.user_id,
                "connection_id": session.connection_id,
            })
            try:
                await asyncio.to_thread(
                    _persist_realtime_poses,
                    [(session.identity, dict(session.pose))],
                )
                session.dirty = False
            except Exception:
                logger.exception("Failed to persist Space position on realtime disconnect")

    def notify_terrain_from_thread(self, world_id: str, revision: int) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._notify_terrain_local, world_id, revision)

    def _notify_terrain_local(self, world_id: str, revision: int) -> None:
        self._record_terrain_revision(world_id, revision)
        self._queue_fanout_event(world_id, {
            "type": "terrain",
            "terrain_revision": revision,
        })

    def _record_terrain_revision(self, world_id: str, revision: int) -> None:
        self.terrain_revisions[world_id] = max(
            revision,
            self.terrain_revisions.get(world_id, 0),
        )

    def update_pose(self, session: RealtimeSession, payload: dict) -> None:
        sequence = int(payload.get("sequence", -1))
        if sequence <= session.last_sequence:
            return
        now = time.monotonic()
        if now - session.rate_window_started_at >= 1:
            session.rate_window_started_at = now
            session.rate_window_packets = 0
        session.rate_window_packets += 1
        # Allow jitter above the advertised 20 Hz while dropping abusive floods.
        if session.rate_window_packets > max(30, settings.SPACE_REALTIME_INPUT_HZ * 2):
            return
        pose = {
            "x_cm": int(payload["x_cm"]),
            "y_cm": int(payload["y_cm"]),
            "z_cm": int(payload["z_cm"]),
            "yaw_q15": int(payload["yaw_q15"]),
            "pitch_q15": int(payload.get("pitch_q15", 0)),
        }
        if not (
            0 <= pose["x_cm"] < session.identity.world_width_cm
            and space_api.MIN_PLAYER_Y_CM <= pose["y_cm"] <= space_api.MAX_PLAYER_Y_CM
            and 0 <= pose["z_cm"] < session.identity.world_length_cm
            and -32767 <= pose["yaw_q15"] <= 32767
            and -32767 <= pose["pitch_q15"] <= 32767
        ):
            raise ValueError("Space player pose is out of bounds")
        session.pose = pose
        session.pose_updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        session.last_sequence = sequence
        session.last_packet_at = now
        session.dirty = True
        self._queue_fanout_event(
            session.identity.world_id,
            self._presence_payload(session),
        )

    @staticmethod
    def _presence_payload(session: RealtimeSession) -> dict:
        pose = session.pose
        if pose is None:
            return {}
        return {
            "type": "pose",
            "connection_id": session.connection_id,
            "user_id": session.identity.user_id,
            "username": session.identity.username,
            "player_entity_id": session.identity.player_entity_id,
            "minecraft_skin_url": session.identity.minecraft_skin_url,
            "minecraft_skin_model": session.identity.minecraft_skin_model,
            "world_width_cm": session.identity.world_width_cm,
            "world_length_cm": session.identity.world_length_cm,
            "x_cm": pose["x_cm"],
            "y_cm": pose["y_cm"],
            "z_cm": pose["z_cm"],
            "yaw_q15": pose["yaw_q15"],
            "pitch_q15": pose["pitch_q15"],
            "updated_at": session.pose_updated_at,
        }

    async def _publish_event(self, world_id: str, payload: dict) -> None:
        if not settings.SPACE_REALTIME_REDIS_FANOUT_ENABLED or not payload:
            return
        try:
            await self.redis.publish(
                f"space:realtime:{world_id}",
                msgpack.packb(payload, use_bin_type=True),
            )
        except Exception:
            now = time.monotonic()
            if now - self.last_redis_warning_at >= 10:
                self.last_redis_warning_at = now
                logger.warning("Space realtime Redis fanout is temporarily unavailable", exc_info=True)

    def _queue_fanout_event(self, world_id: str, payload: dict) -> None:
        if not settings.SPACE_REALTIME_REDIS_FANOUT_ENABLED or not payload:
            return
        if payload.get("type") == "pose" and payload.get("user_id"):
            self.pending_pose_events.setdefault(world_id, {})[str(payload["user_id"])] = payload
            return
        controls = self.pending_control_events.setdefault(world_id, [])
        controls.append(payload)
        if len(controls) > 256:
            del controls[:-256]

    async def _flush_fanout_events(self, world_id: str) -> None:
        poses = list(self.pending_pose_events.pop(world_id, {}).values())
        controls = self.pending_control_events.pop(world_id, [])
        events = [*controls, *poses]
        if events:
            await asyncio.gather(
                *(self._publish_event(world_id, payload) for payload in events),
                return_exceptions=True,
            )

    async def _fanout_loop(self, world_id: str) -> None:
        pubsub = self.redis.pubsub()
        try:
            await pubsub.subscribe(f"space:realtime:{world_id}")
            while self.sessions.get(world_id):
                await self._flush_fanout_events(world_id)
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=0.05,
                )
                if not message or not isinstance(message.get("data"), bytes):
                    await asyncio.sleep(0)
                    continue
                try:
                    payload = _unpack_message(message["data"])
                except (ValueError, msgpack.UnpackException):
                    continue
                message_type = payload.get("type")
                if message_type == "pose" and payload.get("user_id"):
                    payload["received_at"] = time.monotonic()
                    self.remote_states.setdefault(world_id, {})[str(payload["user_id"])] = payload
                elif message_type == "leave" and payload.get("user_id"):
                    user_id = str(payload["user_id"])
                    current = self.remote_states.get(world_id, {}).get(user_id)
                    if current and current.get("connection_id") == payload.get("connection_id"):
                        self.remote_states[world_id].pop(user_id, None)
                elif message_type == "terrain":
                    self._record_terrain_revision(world_id, int(payload.get("terrain_revision", 0)))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Space realtime Redis subscriber stopped; local sessions continue", exc_info=True)
        finally:
            try:
                await pubsub.unsubscribe(f"space:realtime:{world_id}")
                await pubsub.aclose()
            except Exception:
                pass
            self.fanout_tasks.pop(world_id, None)

    @staticmethod
    def _wrapped_delta(a: int, b: int, size: int) -> int:
        delta = abs(a - b)
        return min(delta, size - delta)

    def _players_for(self, observer: RealtimeSession, sessions: list[RealtimeSession]) -> list[dict]:
        if observer.pose is None:
            return []
        radius_cm = settings.SPACE_REALTIME_AOI_RADIUS_CHUNKS * space_api.SPACE_CHUNK_SIZE * 100
        players = []
        for session in sessions:
            if session.pose is None:
                continue
            dx = self._wrapped_delta(
                observer.pose["x_cm"],
                session.pose["x_cm"],
                observer.identity.world_width_cm,
            )
            dz = self._wrapped_delta(
                observer.pose["z_cm"],
                session.pose["z_cm"],
                observer.identity.world_length_cm,
            )
            if session is not observer and dx * dx + dz * dz > radius_cm * radius_cm:
                continue
            pose = session.pose
            players.append({
                "user_id": session.identity.user_id,
                "username": session.identity.username,
                "player_entity_id": session.identity.player_entity_id,
                "minecraft_skin_url": session.identity.minecraft_skin_url,
                "minecraft_skin_model": session.identity.minecraft_skin_model,
                "x_cm": pose["x_cm"],
                "y_cm": pose["y_cm"],
                "z_cm": pose["z_cm"],
                "yaw_q15": pose["yaw_q15"],
                "pitch_q15": pose["pitch_q15"],
                "is_self": session is observer,
                "updated_at": session.pose_updated_at,
            })
        local_user_ids = {session.identity.user_id for session in sessions}
        now = time.monotonic()
        remote_world_states = self.remote_states.get(observer.identity.world_id, {})
        expired_user_ids = []
        for user_id, state in remote_world_states.items():
            if now - float(state.get("received_at", 0)) > SPACE_REALTIME_IDLE_TIMEOUT_SECONDS:
                expired_user_ids.append(user_id)
                continue
            if user_id in local_user_ids:
                continue
            try:
                dx = self._wrapped_delta(
                    observer.pose["x_cm"],
                    int(state["x_cm"]),
                    observer.identity.world_width_cm,
                )
                dz = self._wrapped_delta(
                    observer.pose["z_cm"],
                    int(state["z_cm"]),
                    observer.identity.world_length_cm,
                )
                if dx * dx + dz * dz > radius_cm * radius_cm:
                    continue
                players.append({
                    "user_id": user_id,
                    "username": state["username"],
                    "player_entity_id": state["player_entity_id"],
                    "minecraft_skin_url": state["minecraft_skin_url"],
                    "minecraft_skin_model": state["minecraft_skin_model"],
                    "x_cm": int(state["x_cm"]),
                    "y_cm": int(state["y_cm"]),
                    "z_cm": int(state["z_cm"]),
                    "yaw_q15": int(state["yaw_q15"]),
                    "pitch_q15": int(state.get("pitch_q15", 0)),
                    "is_self": False,
                    "updated_at": state.get("updated_at"),
                })
            except (KeyError, TypeError, ValueError):
                continue
        for user_id in expired_user_ids:
            remote_world_states.pop(user_id, None)
        return players

    async def _send(self, session: RealtimeSession, payload: dict) -> bool:
        try:
            encoded = msgpack.packb(payload, use_bin_type=True)
            await asyncio.wait_for(
                session.websocket.send_bytes(encoded),
                timeout=SPACE_REALTIME_MAX_BUFFERED_SEND_SECONDS,
            )
            return True
        except Exception:
            return False

    async def _world_loop(self, world_id: str) -> None:
        interval = 1 / max(1, settings.SPACE_REALTIME_SNAPSHOT_HZ)
        persistence_interval = max(1, settings.SPACE_REALTIME_PERSIST_SECONDS)
        last_persisted_at = time.monotonic()
        next_fanout_restart_at = 0.0
        server_tick = 0
        try:
            while self.sessions.get(world_id):
                started_at = time.monotonic()
                server_tick += 1
                sessions = list(self.sessions.get(world_id, {}).values())
                now = time.monotonic()
                fanout_task = self.fanout_tasks.get(world_id)
                if (
                    settings.SPACE_REALTIME_REDIS_FANOUT_ENABLED
                    and (fanout_task is None or fanout_task.done())
                    and now >= next_fanout_restart_at
                ):
                    self.fanout_tasks[world_id] = asyncio.create_task(
                        self._fanout_loop(world_id)
                    )
                    next_fanout_restart_at = now + 5
                stale = [
                    session
                    for session in sessions
                    if now - session.last_packet_at > SPACE_REALTIME_IDLE_TIMEOUT_SECONDS
                ]
                for session in stale:
                    try:
                        await session.websocket.close(code=4408, reason="Space realtime connection timed out")
                    except Exception:
                        pass

                send_jobs = []
                for observer in sessions:
                    send_jobs.append(self._send(observer, {
                        "type": "state",
                        "server_tick": server_tick,
                        "players": self._players_for(observer, sessions),
                    }))
                if send_jobs:
                    await asyncio.gather(*send_jobs, return_exceptions=True)

                terrain_revision = self.terrain_revisions.get(world_id, 0)
                if terrain_revision > self.sent_terrain_revisions.get(world_id, 0):
                    terrain_jobs = [
                        self._send(session, {
                            "type": "terrain",
                            "terrain_revision": terrain_revision,
                        })
                        for session in sessions
                    ]
                    if terrain_jobs:
                        await asyncio.gather(*terrain_jobs, return_exceptions=True)
                    self.sent_terrain_revisions[world_id] = terrain_revision

                if now - last_persisted_at >= persistence_interval:
                    dirty = [session for session in sessions if session.dirty and session.pose is not None]
                    records = [(session.identity, dict(session.pose)) for session in dirty]
                    if records:
                        try:
                            await asyncio.to_thread(_persist_realtime_poses, records)
                            for session in dirty:
                                session.dirty = False
                        except Exception:
                            logger.exception("Failed to persist periodic Space realtime positions")
                    last_persisted_at = now

                elapsed = time.monotonic() - started_at
                await asyncio.sleep(max(0, interval - elapsed))
        finally:
            self.tasks.pop(world_id, None)
            if not self.sessions.get(world_id):
                fanout_task = self.fanout_tasks.get(world_id)
                if fanout_task is not None:
                    fanout_task.cancel()
                self.remote_states.pop(world_id, None)
                self.pending_pose_events.pop(world_id, None)
                self.pending_control_events.pop(world_id, None)


realtime_hub = SpaceRealtimeHub()


@api_router.post(
    "/worlds/{world_id}/join-ticket",
    response_model=SpaceJoinTicketResponse,
)
@limiter.limit("30/minute")
def create_space_join_ticket(
    request: Request,
    world_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    world = space_api._require_world_membership(db, str(world_id), current_user)
    return {
        "ticket": _create_join_ticket(str(world.id), current_user.id),
        "websocket_url": settings.SPACE_WS_URL,
        "expires_in_seconds": SPACE_JOIN_TICKET_TTL_SECONDS,
    }


def _unpack_message(data: bytes) -> dict:
    if len(data) > SPACE_REALTIME_MAX_MESSAGE_BYTES:
        raise ValueError("Space realtime message is too large")
    payload = msgpack.unpackb(data, raw=False, strict_map_key=False)
    if not isinstance(payload, dict):
        raise ValueError("Space realtime message must be a map")
    return payload


async def _receive_binary(websocket: WebSocket, timeout: float | None = None) -> bytes:
    receive = websocket.receive()
    message = await asyncio.wait_for(receive, timeout=timeout) if timeout else await receive
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    data = message.get("bytes")
    if data is None:
        raise ValueError("Space realtime accepts binary messages only")
    return data


@realtime_router.websocket("/space/ws/v2")
async def space_realtime(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin not in _allowed_origins() and (_is_production() or origin is not None):
        await websocket.close(code=4403, reason="Origin is not allowed")
        return
    if SPACE_REALTIME_PROTOCOL not in websocket.scope.get("subprotocols", []):
        await websocket.close(code=4406, reason="Unsupported Space realtime protocol")
        return

    await websocket.accept(subprotocol=SPACE_REALTIME_PROTOCOL)
    session: RealtimeSession | None = None
    try:
        hello = _unpack_message(await _receive_binary(websocket, timeout=5))
        if hello.get("type") != "hello" or not isinstance(hello.get("ticket"), str):
            await websocket.close(code=4401, reason="A valid Space join ticket is required")
            return
        identity = await asyncio.to_thread(_authenticate_realtime_ticket, hello["ticket"])
        session = RealtimeSession(websocket=websocket, identity=identity)
        await realtime_hub.register(session)
        await websocket.send_bytes(msgpack.packb({
            "type": "hello",
            "protocol": SPACE_REALTIME_PROTOCOL,
            "input_hz": settings.SPACE_REALTIME_INPUT_HZ,
            "snapshot_hz": settings.SPACE_REALTIME_SNAPSHOT_HZ,
            "persistence_seconds": settings.SPACE_REALTIME_PERSIST_SECONDS,
        }, use_bin_type=True))

        while True:
            payload = _unpack_message(await _receive_binary(websocket))
            message_type = payload.get("type")
            if message_type == "pose":
                try:
                    realtime_hub.update_pose(session, payload)
                except (KeyError, TypeError, ValueError, HTTPException):
                    await websocket.close(code=4400, reason="Invalid Space player pose")
                    return
            elif message_type == "ping":
                session.last_packet_at = time.monotonic()
                await websocket.send_bytes(msgpack.packb({
                    "type": "pong",
                    "client_time": payload.get("client_time"),
                }, use_bin_type=True))
            elif message_type == "leave":
                # Persist before acknowledging a graceful close so callers can
                # rely on the reconnect checkpoint being durable.
                await realtime_hub.unregister(session)
                session = None
                await websocket.close(code=1000, reason="Space session closed")
                return
            else:
                await websocket.close(code=4400, reason="Unknown Space realtime message")
                return
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except HTTPException as exc:
        try:
            await websocket.close(code=4401, reason=str(exc.detail))
        except Exception:
            pass
    except Exception:
        logger.exception("Space realtime connection failed")
        try:
            await websocket.close(code=1011, reason="Space realtime server error")
        except Exception:
            pass
    finally:
        if session is not None:
            await realtime_hub.unregister(session)
