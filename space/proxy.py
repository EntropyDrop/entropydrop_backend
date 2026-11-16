"""Compatibility forwarding for clients still using the cloud Space URL."""
import asyncio
import hmac
import ipaddress
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy
from time import perf_counter
from urllib.parse import urlsplit
import httpx
import websockets
from anyio import CancelScope
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, StreamingResponse
from config import settings
from rate_limit import limiter, _is_trusted_proxy

@asynccontextmanager
async def lifespan(app):
    # One pool per API process/event loop. Never inherit desktop proxy settings
    # or retain upstream cookies across callers of this shared client.
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(35, connect=5),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30),
        follow_redirects=False,
        trust_env=False,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    ) as client:
        app.state.space_proxy_client = client
        try:
            yield
        finally:
            del app.state.space_proxy_client


router = APIRouter(lifespan=lifespan)
HOP_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "cookie", "set-cookie"}


class UpstreamStreamingResponse(StreamingResponse):
    """Release the pool slot even when streaming fails or the caller disconnects."""

    def __init__(self, upstream, headers):
        super().__init__(upstream.aiter_raw(), status_code=upstream.status_code, headers=headers)
        self.upstream = upstream

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with CancelScope(shield=True):
                await self.upstream.aclose()


def upstream_timing(started_at):
    # Includes pool wait, connection setup, request upload, network and Space
    # processing, ending at upstream response headers (not the streamed body).
    return f'gateway_upstream;dur={(perf_counter() - started_at) * 1000:.2f}'


def client_ip(request):
    peer = request.client.host if request.client else "127.0.0.1"
    supplied = request.headers.get("x-space-edge-token", "")
    if settings.SPACE_EDGE_TOKEN and hmac.compare_digest(supplied, settings.SPACE_EDGE_TOKEN):
        candidate = request.headers.get("x-space-client-ip", peer)
    elif _is_trusted_proxy(peer):
        # ALB appends the actual immediate client. Discard viewer-supplied prefixes.
        candidate = request.headers.get("x-forwarded-for", peer).split(",")[-1].strip()
    else:
        candidate = peer
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


def target(path):
    base = settings.SPACE_SERVICE_URL.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path:
        raise RuntimeError("SPACE_SERVICE_URL must be a trusted origin without a path")
    return base + path


@router.api_route("/space/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@limiter.exempt
async def forward(request: Request, path: str):
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    headers.pop("x-space-service-token", None)
    for name in ("x-real-ip", "x-forwarded-for", "x-space-client-ip", "x-space-edge-token"):
        headers.pop(name, None)
    headers["x-real-ip"] = client_ip(request)
    headers["accept-encoding"] = "identity"
    client = request.app.state.space_proxy_client
    body = await request.body()
    upstream_request = client.build_request(request.method,
        target(request.url.path), params=request.query_params.multi_items(), headers=headers,
        content=body)
    started_at = perf_counter()
    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        return JSONResponse({"detail": {"code": "SPACE_SERVICE_UNAVAILABLE"}}, status_code=503,
            headers={"Cache-Control": "no-store", "Server-Timing": upstream_timing(started_at)})
    try:
        timing = upstream_timing(started_at)
        response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
        if "location" in response_headers:
            location = urlsplit(response_headers["location"])
            if location.netloc == urlsplit(settings.SPACE_SERVICE_URL).netloc:
                scheme = "https" if settings.SPACE_PUBLIC_API_URL.startswith("https://") else request.url.scheme
                response_headers["location"] = location._replace(scheme=scheme, netloc=request.url.netloc).geturl()
        response_headers["cache-control"] = "no-store"
        existing_timing = response_headers.get("server-timing")
        response_headers["server-timing"] = f"{existing_timing}, {timing}" if existing_timing else timing
        return UpstreamStreamingResponse(upstream, response_headers)
    except BaseException:
        with CancelScope(shield=True):
            await upstream.aclose()
        raise


@router.websocket("/space/ws/v2/entity-messages")
@router.websocket("/space/ws/v2")
async def forward_socket(socket: WebSocket):
    url = target(socket.url.path).replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    if socket.url.query:
        url += "?" + socket.url.query
    protocols = [p.strip() for p in socket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
    tasks = []
    try:
        async with websockets.connect(url, origin=socket.headers.get("origin"), subprotocols=protocols,
                max_size=1024 * 1024, open_timeout=5, proxy=None) as upstream:
            await socket.accept(subprotocol=upstream.subprotocol)
            async def incoming():
                while True:
                    message = await socket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(message.get("bytes") if message.get("bytes") is not None else message["text"])
            async def outgoing():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await socket.send_bytes(message)
                    else:
                        await socket.send_text(message)
            tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (OSError, WebSocketDisconnect, websockets.exceptions.WebSocketException):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await socket.close(code=1013)
        except RuntimeError:
            pass
