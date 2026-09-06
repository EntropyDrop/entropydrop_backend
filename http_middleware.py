"""HTTP limits shared with the standalone Space service."""
import logging
import os
from fastapi import Request
from starlette.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from rate_limit import limiter
logger = logging.getLogger(__name__)

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


async def unhandled_exception_handler(request, exc):
    log_unhandled_exception(exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


def configure_http(app):
    app.state.limiter = limiter
    app.middleware("http")(limit_upload_size)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
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
