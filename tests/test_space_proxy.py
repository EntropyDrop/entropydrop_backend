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
