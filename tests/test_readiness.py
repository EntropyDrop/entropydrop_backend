def test_readiness_ok(client, monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "check_readiness_dependencies",
        lambda: {"database": "ok", "redis": "ok"},
    )

    response = client.get("/skin/api/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {"database": "ok", "redis": "ok"},
    }


def test_readiness_dependency_failure(client, monkeypatch):
    import main

    monkeypatch.setattr(
        main,
        "check_readiness_dependencies",
        lambda: {"database": "ok", "redis": "error: TimeoutError"},
    )

    response = client.get("/skin/api/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "dependencies": {"database": "ok", "redis": "error: TimeoutError"},
    }
