import pytest
from config import settings


def test_system_probe_endpoints(client):
    # Root
    assert client.get("/").json() == {"message": "Welcome to ED Backend API!"}
    assert client.get("/api").json() == {"message": "Welcome to ED Backend API!"}
    assert client.get("/skin").json() == {"message": "Welcome to ED Backend API!"}

    # Health
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok", "service": "ed_backend"}
    assert client.get("/api/health").status_code == 200
    assert client.get("/skin/api/health").status_code == 200

    # Ready
    assert client.get("/ready").status_code == 200
    assert client.get("/ready").json()["status"] == "ready"
    assert client.get("/api/ready").status_code == 200
    assert client.get("/skin/api/ready").status_code == 200

    # Version
    assert client.get("/version").status_code == 200
    assert "version" in client.get("/version").json()
    assert client.get("/api/version").status_code == 200
    assert client.get("/skin/api/version").status_code == 200


def test_standard_and_legacy_core_routes(client):
    # Unauthenticated /api/auth/me vs /skin/api/auth/me
    std_auth = client.get("/api/users/me")
    legacy_auth = client.get("/skin/api/users/me")
    assert std_auth.status_code == 401
    assert legacy_auth.status_code == 401
    assert std_auth.json() == legacy_auth.json()

    # Public ledger
    std_ledger = client.get("/api/public/ledger")
    legacy_ledger = client.get("/skin/api/public/ledger")
    assert std_ledger.status_code == 200
    assert legacy_ledger.status_code == 200
    assert std_ledger.json() == legacy_ledger.json()

    # Credit packages
    std_packages = client.get("/api/credits/packages")
    legacy_packages = client.get("/skin/api/credits/packages")
    assert std_packages.status_code == 200
    assert legacy_packages.status_code == 200
    assert std_packages.json() == legacy_packages.json()


def test_auth_cookie_path_matches_calling_convention(client, monkeypatch):
    import auth as auth_module

    monkeypatch.setattr(auth_module, "verify_google_token", lambda token: {
        "email": "route-test@example.com",
        "sub": "google-route-test-123",
        "email_verified": True,
        "name": "Route Test",
    })

    # Standard /api login sets Path=/api/auth
    std_login = client.post("/api/auth/google", json={"token": "mock-token"})
    assert std_login.status_code == 200
    assert "Path=/api/auth" in std_login.headers["set-cookie"]

    # Legacy /skin login sets Path=/skin/api/auth
    legacy_login = client.post("/skin/api/auth/google", json={"token": "mock-token"})
    assert legacy_login.status_code == 200
    assert "Path=/skin/api/auth" in legacy_login.headers["set-cookie"]

    # Logout clears both paths
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 200
    raw_cookies = logout.headers.get_list("set-cookie") if hasattr(logout.headers, "get_list") else [logout.headers.get("set-cookie", "")]
    combined_cookies = " ".join(raw_cookies)
    assert "Path=/api/auth" in combined_cookies
    assert "Path=/skin/api/auth" in combined_cookies
