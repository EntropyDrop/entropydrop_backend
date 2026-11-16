from starlette.requests import Request
from config import settings
from space.proxy import client_ip
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
from space import proxy


def proxy_app():
    app = FastAPI()
    app.include_router(proxy.router)
    return app


def test_proxy_reuses_tcp_connection_without_sharing_cookies_or_credentials(monkeypatch):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            observed.append((self.client_address, self.path,
                             {name.lower(): value for name, value in self.headers.items()}))
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "upstream_session=private; Path=/")
            self.send_header("Server-Timing", "space_app;dur=2.50")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(settings, "SPACE_SERVICE_URL", f"http://127.0.0.1:{server.server_port}")
    # Environment proxies must not intercept this trusted upstream.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    app = proxy_app()
    try:
        with TestClient(app) as client:
            pool = app.state.space_proxy_client
            for token in ("first-user", "second-user"):
                response = client.get("/space/health?x=1&x=2", headers={
                    "Authorization": f"Bearer {token}", "Cookie": "viewer_session=private",
                })
                assert response.status_code == 200
                assert response.json() == {"ok": True}
                assert "set-cookie" not in response.headers
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["server-timing"].startswith("space_app;dur=2.50, gateway_upstream;dur=")
                assert app.state.space_proxy_client is pool and not pool.is_closed
            assert not pool.cookies
        assert pool.is_closed
        assert not hasattr(app.state, "space_proxy_client")
        assert observed[0][0] == observed[1][0]  # Actual TCP source port, not just the client object.
        assert all(path == "/space/health?x=1&x=2" for _, path, _ in observed)
        assert [headers["authorization"] for _, _, headers in observed] == [
            "Bearer first-user", "Bearer second-user",
        ]
        assert all("cookie" not in headers for _, _, headers in observed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_proxy_timeout_preserves_pool_for_next_request(monkeypatch):
    clients = []
    requests = []
    original_client = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("upstream unavailable", request=request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'))

    def create_client(**kwargs):
        client = original_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", create_client)
    monkeypatch.setattr(settings, "SPACE_SERVICE_URL", "http://space.test")
    with TestClient(proxy_app()) as client:
        failed = client.get("/space/health")
        assert failed.status_code == 503
        assert failed.json()["detail"]["code"] == "SPACE_SERVICE_UNAVAILABLE"
        assert failed.headers["cache-control"] == "no-store"
        assert failed.headers["server-timing"].startswith("gateway_upstream;dur=")
        assert not clients[0].is_closed
        assert client.get("/space/health").status_code == 200
        assert len(clients) == 1
    assert clients[0].is_closed


@pytest.mark.parametrize("failure", ["upstream", "disconnect", "cancel"])
def test_proxy_releases_upstream_on_interrupted_stream(failure):
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"first chunk"
            raise httpx.ReadError("upstream stream broke")

        async def aclose(self):
            await asyncio.sleep(0)
            self.closed = True

    async def run():
        stream = Stream()
        upstream = httpx.Response(200, stream=stream)
        response = proxy.UpstreamStreamingResponse(upstream, {})

        async def receive():
            raise AssertionError("ASGI 2.4 does not need a disconnect listener")

        async def send(message):
            if message["type"] == "http.response.body":
                if failure == "disconnect":
                    raise OSError("downstream disconnected")
                if failure == "cancel":
                    raise asyncio.CancelledError()

        expected = {"upstream": httpx.ReadError, "disconnect": ClientDisconnect,
                    "cancel": asyncio.CancelledError}[failure]
        with pytest.raises(expected):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert upstream.is_closed and stream.closed

    asyncio.run(run())


def test_gateway_cors_exposes_timing_header():
    from main import app
    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert "Server-Timing" in response.headers["access-control-expose-headers"]


def request(headers):
    return Request({'type':'http','method':'GET','path':'/space/health','headers':[(k.encode(),v.encode()) for k,v in headers.items()],
        'client':('10.23.1.2',1234),'server':('accounts.example.test',443),'scheme':'https','query_string':b''})


def test_proxy_client_address_requires_authenticated_edge(monkeypatch):
    monkeypatch.setattr(settings,'SPACE_EDGE_TOKEN','trusted-edge')
    monkeypatch.setattr(settings,'TRUSTED_PROXY_CIDRS','10.23.0.0/16')
    headers={'x-real-ip':'1.1.1.1','x-forwarded-for':'2.2.2.2, 203.0.113.10','x-space-client-ip':'192.0.2.10'}
    assert client_ip(request(headers))=='203.0.113.10'
    assert client_ip(request({**headers,'x-space-edge-token':'forged'}))=='203.0.113.10'
    assert client_ip(request({**headers,'x-space-edge-token':'trusted-edge'}))=='192.0.2.10'
    monkeypatch.setattr(settings,'TRUSTED_PROXY_CIDRS','')
    assert client_ip(request(headers))=='10.23.1.2'


def test_proxy_forwards_entity_message_socket(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from space import proxy

    observed = {}
    protocol = "space-entity-messages-v1"

    class Upstream:
        subprotocol = protocol

        def __init__(self):
            self.messages = asyncio.Queue()

        async def send(self, message):
            await self.messages.put(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

    @asynccontextmanager
    async def connect(url, **kwargs):
        observed.update(url=url, **kwargs)
        yield Upstream()

    monkeypatch.setattr(settings, "SPACE_SERVICE_URL", "http://space.test:18081")
    monkeypatch.setattr(proxy.websockets, "connect", connect)
    app = FastAPI()
    app.include_router(proxy.router)
    with TestClient(app) as client:
        with client.websocket_connect("/space/ws/v2/entity-messages", subprotocols=[protocol],
                                      headers={"origin": "http://localhost:5173"}) as socket:
            assert socket.accepted_subprotocol == protocol
            socket.send_bytes(b"native-click-frame")
            assert socket.receive_bytes() == b"native-click-frame"
            socket.send_text("ping")
            assert socket.receive_text() == "ping"
            socket.close()
            assert socket.receive()["type"] == "websocket.close"
    assert observed["url"] == "ws://space.test:18081/space/ws/v2/entity-messages"
    assert observed["origin"] == "http://localhost:5173"
    assert observed["subprotocols"] == [protocol]
    assert observed["proxy"] is None
