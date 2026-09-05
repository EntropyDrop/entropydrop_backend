from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from redis import Redis
from sqlalchemy import text

from database import engine
import models
from routers import (
    address,
    auth,
    collections,
    credit,
    forum,
    generate,
    ledger,
    monitor,
    order,
    space,
    space_entities,
    space_hosting,
    space_external,
    space_market,
    space_realtime,
    webhooks,
)
from config import settings
from instance_monitor import run_backend_instance_heartbeat

from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from rate_limit import limiter
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

if settings.AUTO_CREATE_TABLES:
    models.Base.metadata.create_all(bind=engine)


readiness_redis = Redis.from_url(
    settings.REDIS_URL,
    health_check_interval=20,
    socket_timeout=3,
    socket_connect_timeout=3,
    retry_on_timeout=True,
)


def check_readiness_dependencies():
    status = {
        "database": "ok",
        "redis": "ok",
    }

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        status["database"] = f"error: {exc.__class__.__name__}"

    try:
        readiness_redis.ping()
    except Exception as exc:
        status["redis"] = f"error: {exc.__class__.__name__}"

    return status


@asynccontextmanager
async def lifespan(app: FastAPI):
    interval_seconds = max(2, settings.BACKEND_METRICS_INTERVAL_SECONDS)
    stale_after_seconds = max(1, settings.BACKEND_METRICS_STALE_AFTER_SECONDS)
    task = asyncio.create_task(
        run_backend_instance_heartbeat(
            readiness_redis,
            interval_seconds,
            max(
                settings.BACKEND_METRICS_TTL_SECONDS,
                interval_seconds * 3,
                stale_after_seconds + 1,
            ),
            check_readiness_dependencies,
            max(1, settings.BACKEND_METRICS_HISTORY_HOURS),
            max(60, settings.BACKEND_METRICS_HISTORY_BUCKET_SECONDS),
        )
    )
    app.state.backend_instance_monitor_task = task
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass



app = FastAPI(
    title="ED Backend API",
    description="Backend services for the ED project",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

app.state.limiter = limiter

DEFAULT_REQUEST_BODY_LIMIT_BYTES = 512 * 1024
SPACE_MARKET_REQUEST_BODY_LIMIT_BYTES = 9 * 1024 * 1024
SPACE_ENTITY_REQUEST_BODY_LIMIT_BYTES = 17 * 1024 * 1024


def _is_space_market_publish(request: Request) -> bool:
    return (
        request.method == "POST"
        and request.url.path.rstrip("/") == "/space/api/v2/market/resources"
    )


def _is_space_entity_definition_write(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    return (
        path.startswith("/space/api/v2/worlds/")
        and (
            (
                request.method == "POST"
                and (path.endswith("/entities") or path.endswith("/entities/browser"))
            )
            or (request.method == "PUT" and path.endswith("/checkpoint"))
        )
    )


def _request_too_large_response(request_kind: str) -> JSONResponse:
    if request_kind == "market":
        return JSONResponse(
            status_code=413,
            content={"detail": {
                "code": "MARKET_RESOURCE_TOO_LARGE",
                "message": "Market resources may not exceed 8 MiB after canonicalization.",
            }},
        )
    if request_kind == "entity":
        return JSONResponse(
            status_code=413,
            content={"detail": {
                "code": "ENTITY_DEFINITION_TOO_LARGE",
                "message": "The entity definition or checkpoint exceeds the request limit.",
            }},
        )
    return JSONResponse(
        status_code=413,
        content={"detail": "Request entity too large (Max 512KB)"},
    )

def log_unhandled_exception(exc):
    logger.error(
        "Unhandled request error",
        exc_info=(type(exc), exc, exc.__traceback__),
    )

@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    is_market_publish = _is_space_market_publish(request)
    is_entity_write = _is_space_entity_definition_write(request)
    if request.method in ["POST", "PUT", "PATCH"]:
        request_kind = "market" if is_market_publish else "entity" if is_entity_write else "default"
        limit = {
            "market": SPACE_MARKET_REQUEST_BODY_LIMIT_BYTES,
            "entity": SPACE_ENTITY_REQUEST_BODY_LIMIT_BYTES,
            "default": DEFAULT_REQUEST_BODY_LIMIT_BYTES,
        }[request_kind]
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})
            if declared_length < 0:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})
            if declared_length > limit:
                return _request_too_large_response(request_kind)

        received = 0
        buffered_messages = []
        original_receive = request._receive
        while True:
            message = await original_receive()
            if message.get("type") != "http.request":
                buffered_messages.append(message)
                break
            received += len(message.get("body", b""))
            if received > limit:
                return _request_too_large_response(request_kind)
            buffered_messages.append(message)
            if not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive():
            nonlocal message_index
            if message_index < len(buffered_messages):
                message = buffered_messages[message_index]
                message_index += 1
                return message
            return await original_receive()

        request._receive = replay_receive
    try:
        return await call_next(request)
    except Exception as exc:
        log_unhandled_exception(exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"}
        )

def rate_limit_exceeded_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests."}
    )

app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    log_unhandled_exception(exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )

# CORS configuration
_default_origins = [
    "https://entropydrop.com",
    "https://www.entropydrop.com",
    "http://localhost:5173",
    "http://localhost:3000",
]
_cors_origins = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "").split(",")
    if o.strip()
] or _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SlowAPIMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)



# Register routers
app.include_router(auth.router, prefix="/skin")
app.include_router(generate.router, prefix="/skin")
app.include_router(collections.router, prefix="/skin")
app.include_router(address.router, prefix="/skin")
app.include_router(order.router, prefix="/skin")
app.include_router(webhooks.router, prefix="/skin")
app.include_router(monitor.router, prefix="/skin")
app.include_router(ledger.router, prefix="/skin")
app.include_router(ledger.legacy_open_router, prefix="/skin")
app.include_router(forum.router, prefix="/skin")
app.include_router(credit.router, prefix="/skin")
app.include_router(space.router)
app.include_router(space_entities.router)
app.include_router(space_entities.api_key_router)
app.include_router(space_hosting.router)
app.include_router(space_external.router)
app.include_router(space_market.router)
app.include_router(space_realtime.api_router)
app.include_router(space_realtime.realtime_router)



@app.get("/skin")
@limiter.exempt
async def root():
    return {"message": "Welcome to ED Backend API!"}

@app.get("/skin/api/health")
@limiter.exempt
async def health_check():
    return {"status": "ok", "service": "ed_backend"}

@app.get("/skin/api/ready")
@limiter.exempt
async def readiness_check():
    dependencies = await asyncio.to_thread(check_readiness_dependencies)
    ready = all(value == "ok" for value in dependencies.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "dependencies": dependencies,
        }
    )

@app.get("/skin/api/version")
@limiter.exempt
async def get_version():
    return {
        "version": app.version,
        "deploy_time": os.getenv("DEPLOY_TIME", "unknown"),
        "git_commit": os.getenv("GIT_COMMIT", "unknown")
    }
