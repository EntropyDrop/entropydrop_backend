import asyncio

import background_service


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, num_keys, key, token, ttl=None):
        if self.values.get(key) != token:
            return 0
        if "expire" in script:
            return 1
        if "del" in script:
            del self.values[key]
            return 1
        return 0


def test_background_lock_uses_token_for_renew_and_release(monkeypatch):
    monkeypatch.setattr(background_service, "LOCK_KEY", "test:background:lock")
    redis_conn = FakeRedis()

    assert background_service.acquire_lock(redis_conn, "owner-a") is True
    assert background_service.acquire_lock(redis_conn, "owner-b") is False

    assert background_service.renew_lock(redis_conn, "owner-b") is False
    assert background_service.renew_lock(redis_conn, "owner-a") is True

    background_service.release_lock(redis_conn, "owner-b")
    assert redis_conn.values["test:background:lock"] == "owner-a"

    background_service.release_lock(redis_conn, "owner-a")
    assert "test:background:lock" not in redis_conn.values


def test_run_with_lock_cancels_workers_when_lock_is_lost(monkeypatch):
    async def run_test():
        worker_started = asyncio.Event()
        worker_cancelled = asyncio.Event()

        async def fake_background_tasks():
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                worker_cancelled.set()

        monkeypatch.setattr(background_service, "LOCK_RENEW_SECONDS", 0)
        monkeypatch.setattr(background_service, "renew_lock", lambda redis_conn, token: False)
        monkeypatch.setattr(background_service, "run_background_tasks", fake_background_tasks)

        try:
            await background_service.run_with_lock(object(), "token")
        except RuntimeError as exc:
            assert "singleton lock was lost" in str(exc)
        else:
            raise AssertionError("run_with_lock should fail when the singleton lock is lost")

        assert worker_started.is_set()
        assert worker_cancelled.is_set()

    asyncio.run(run_test())
