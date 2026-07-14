from starlette.requests import Request

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
            "X-Forwarded-For": "203.0.113.30, 10.1.2.3",
        },
    )

    assert rate_limit.get_real_remote_address(request) == "203.0.113.30"


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
