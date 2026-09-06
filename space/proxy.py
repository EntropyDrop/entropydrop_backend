"""Compatibility forwarding for clients still using the cloud Space URL."""
import asyncio
import hmac
import ipaddress
from urllib.parse import urlsplit
import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from config import settings
from rate_limit import limiter, _is_trusted_proxy

router = APIRouter()
HOP_HEADERS = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "cookie", "set-cookie"}


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
    # This trusted origin is reached directly over LAN/SSH/private VPC. Desktop
    # proxy settings must not redirect account credentials to another proxy.
    client = httpx.AsyncClient(timeout=httpx.Timeout(35, connect=5), follow_redirects=False, trust_env=False)
    try:
        upstream = await client.send(client.build_request(request.method,
            target(request.url.path), params=request.query_params.multi_items(), headers=headers,
            content=await request.body()), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse({"detail": {"code": "SPACE_SERVICE_UNAVAILABLE"}}, status_code=503)
    async def close():
        await upstream.aclose()
        await client.aclose()
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
    if "location" in response_headers:
        location = urlsplit(response_headers["location"])
        if location.netloc == urlsplit(settings.SPACE_SERVICE_URL).netloc:
            scheme = "https" if settings.SPACE_PUBLIC_API_URL.startswith("https://") else request.url.scheme
            response_headers["location"] = location._replace(scheme=scheme, netloc=request.url.netloc).geturl()
    response_headers["cache-control"] = "no-store"
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
        headers=response_headers, background=BackgroundTask(close))


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
