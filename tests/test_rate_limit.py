from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import jwt

import rate_limit


def make_request(client_host: str, headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    })


def test_rate_limit_ignores_forwarded_headers_from_untrusted_clients(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = make_request(
        "198.51.100.10",
        {
            "X-Real-IP": "203.0.113.20",
            "X-Forwarded-For": "203.0.113.30",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "198.51.100.10"


def test_rate_limit_uses_forwarded_headers_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = make_request(
        "10.1.2.3",
        {
            "X-Real-IP": "10.2.3.4",
            "X-Forwarded-For": "203.0.113.30, 10.9.8.7",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "203.0.113.30"


def test_rate_limit_discards_spoofed_forwarded_prefix(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = make_request(
        "10.1.2.3",
        {
            "X-Forwarded-For": "192.0.2.99, 203.0.113.30",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "203.0.113.30"


def test_rate_limit_falls_back_to_real_ip_for_malformed_forwarded_chain(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = make_request(
        "10.1.2.3",
        {
            "X-Real-IP": "203.0.113.20",
            "X-Forwarded-For": "spoofed, 203.0.113.30",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "203.0.113.20"


def test_rate_limit_rejects_malformed_forwarded_header(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "TRUSTED_PROXY_CIDRS", "10.0.0.0/8")
    request = make_request(
        "10.1.2.3",
        {
            "X-Real-IP": "not-an-ip",
            "X-Forwarded-For": "also-not-an-ip",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "10.1.2.3"


def test_authenticated_rate_limit_uses_verified_user_subject(monkeypatch):
    secret = "test-secret-at-least-thirty-two-bytes-long"
    monkeypatch.setattr(rate_limit.settings, "JWT_SECRET_KEY", secret)
    monkeypatch.setattr(rate_limit.settings, "JWT_ALGORITHM", "HS256")
    token = jwt.encode(
        {"sub": "user-123", "type": "access"},
        secret,
        algorithm="HS256",
    )
    request = make_request(
        "198.51.100.10",
        {"Authorization": f"Bearer {token}"},
    )

    assert rate_limit.get_authenticated_or_remote_address(request) == "user:user-123"


def test_authenticated_rate_limit_rejects_unverified_subject(monkeypatch):
    monkeypatch.setattr(
        rate_limit.settings,
        "JWT_SECRET_KEY",
        "test-secret-at-least-thirty-two-bytes-long",
    )
    monkeypatch.setattr(rate_limit.settings, "JWT_ALGORITHM", "HS256")
    token = jwt.encode(
        {"sub": "forged-user", "type": "access"},
        "different-secret-at-least-thirty-two-bytes",
        algorithm="HS256",
    )
    request = make_request(
        "198.51.100.10",
        {"Authorization": f"Bearer {token}"},
    )

    assert rate_limit.get_authenticated_or_remote_address(request) == "198.51.100.10"


def test_limiter_uses_endpoint_scopes_and_response_headers():
    assert rate_limit.limiter._key_style == "endpoint"
    assert rate_limit.limiter._headers_enabled is True
    assert rate_limit.limiter._application_limits


def test_decorated_route_shares_dynamic_path_bucket_and_emits_headers():
    test_limiter = rate_limit.HeaderSafeLimiter(
        key_func=lambda request: "test-client",
        headers_enabled=True,
        key_style="endpoint",
    )
    app = FastAPI()
    app.state.limiter = test_limiter

    @app.get("/items/{item_id}")
    @test_limiter.limit("1/minute")
    def item(request: Request, item_id: str):
        return {"item_id": item_id}

    def exceeded(request, exc):
        response = JSONResponse({"detail": "Too many requests."}, status_code=429)
        return test_limiter._inject_headers(response, request.state.view_rate_limit)

    app.add_exception_handler(RateLimitExceeded, exceeded)
    app.add_middleware(SlowAPIMiddleware)
    app.middleware("http")(rate_limit.ensure_rate_limit_headers)

    with TestClient(app) as client:
        first = client.get("/items/one")
        second = client.get("/items/two")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "1"
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


def test_decorated_routes_still_enforce_application_wide_limit():
    test_limiter = rate_limit.HeaderSafeLimiter(
        key_func=lambda request: "test-client",
        application_limits=["1/minute"],
        key_style="endpoint",
    )
    app = FastAPI()
    app.state.limiter = test_limiter

    @app.get("/first")
    @test_limiter.limit("10/minute")
    def first(request: Request):
        return {"ok": True}

    @app.get("/second")
    @test_limiter.limit("10/minute")
    def second(request: Request):
        return {"ok": True}

    app.add_middleware(SlowAPIMiddleware)

    with TestClient(app) as client:
        assert client.get("/first").status_code == 200
        assert client.get("/second").status_code == 429


def test_decorated_routes_do_not_enforce_default_limits():
    test_limiter = rate_limit.HeaderSafeLimiter(
        key_func=lambda request: "test-client",
        default_limits=["5/minute"],
        application_limits=["100/minute"],
        key_style="endpoint",
    )
    app = FastAPI()
    app.state.limiter = test_limiter

    @app.get("/heavy")
    @test_limiter.limit("20/minute")
    def heavy(request: Request):
        return {"ok": True}

    app.add_middleware(SlowAPIMiddleware)

    with TestClient(app) as client:
        for i in range(1, 21):
            assert client.get("/heavy").status_code == 200, f"Request {i} failed unexpectedly"
        assert client.get("/heavy").status_code == 429
