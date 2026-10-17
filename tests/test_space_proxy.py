from starlette.requests import Request
from config import settings
from space.proxy import client_ip


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
