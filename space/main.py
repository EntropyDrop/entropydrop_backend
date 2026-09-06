"""Independent Space API: local world data, remotely verified cloud accounts."""
import asyncio
from fastapi import FastAPI, Depends, HTTPException
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, Response
from config import settings
from space.database import engine, get_db
from space import models
from http_middleware import configure_http
from rate_limit import limiter
from routers import space, space_entities, space_hosting, space_external, space_market, space_realtime

if not settings.SPACE_STANDALONE:
    raise RuntimeError("space.main requires SPACE_STANDALONE=true")
if not settings.SPACE_JOIN_TICKET_SECRET:
    raise RuntimeError("SPACE_JOIN_TICKET_SECRET is required")

app = FastAPI(title="EntropyDrop Space API", docs_url=None, redoc_url=None, openapi_url=None)
configure_http(app)
for router in (space.router, space_entities.router, space_hosting.router, space_external.router,
               space_market.router, space_realtime.api_router, space_realtime.realtime_router):
    app.include_router(router)

redis = Redis.from_url(settings.REDIS_URL, socket_timeout=3, socket_connect_timeout=3)


def dependencies():
    status = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception:
        status["database"] = "unavailable"
    try:
        redis.ping()
        status["redis"] = "ok"
    except Exception:
        status["redis"] = "unavailable"
    return status


@app.get("/space/health")
@limiter.exempt
async def health():
    return {"status": "ok", "service": "space"}


@app.get("/space/ready")
@limiter.exempt
async def ready():
    status = await asyncio.to_thread(dependencies)
    ok = all(value == "ok" for value in status.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "dependencies": status}, status_code=200 if ok else 503)


@app.get("/space/objects/{key:path}")
def object_content(key: str, db: Session = Depends(get_db)):
    from space.object_store import object_path
    resource = db.query(models.SpaceMarketResource).filter_by(object_key=key).first()
    if resource is None:
        raise HTTPException(404)
    try:
        content = object_path(key).read_bytes()
    except (FileNotFoundError, ValueError):
        raise HTTPException(404)
    return Response(content, media_type="application/x-protobuf", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
