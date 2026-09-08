import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

import background_service
import space_surface


@pytest.mark.parametrize("space_url", ["", "http://space:8000"])
def test_background_jobs_respect_standalone_space_cutover(monkeypatch, space_url):
    monkeypatch.setattr(background_service.settings, "SPACE_SERVICE_URL", space_url)
    monkeypatch.setattr(background_service.order, "repair_unhandled_orders", AsyncMock())

    async def run_test():
        started, cancelled = set(), set()
        expected = {"discovery", "results", "recovery", "ledger"}
        if not space_url:
            expected.add("surface")
        ready = asyncio.Event()

        async def job(name):
            started.add(name)
            if expected <= started:
                ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.add(name)

        for target, method, name in (
            (background_service.generate, "start_discovery_cache_job", "discovery"),
            (background_service.generate, "start_result_listener", "results"),
            (background_service.generate, "start_pending_recovery_job", "recovery"),
            (background_service.ledger, "start_ledger_sync_job", "ledger"),
            (space_surface, "start_surface_snapshot_job", "surface"),
        ):
            monkeypatch.setattr(target, method, lambda name=name: job(name))
        supervisor = asyncio.create_task(background_service.run_background_tasks())
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
            await asyncio.sleep(0)
            assert started == expected
        finally:
            supervisor.cancel()
            with pytest.raises(asyncio.CancelledError):
                await supervisor
        assert cancelled == expected

    asyncio.run(run_test())


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


def test_surface_manifest_warmup_starts_only_one_daemon(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fake_backfill():
        started.set()
        release.wait(timeout=2)

    monkeypatch.setattr(space_surface, "_generation_thread", None)
    monkeypatch.setattr(space_surface, "_run_surface_generation_until_current", fake_backfill)

    assert space_surface.ensure_surface_generation_started() is True
    assert started.wait(timeout=1)
    assert space_surface.ensure_surface_generation_started() is False

    release.set()
    space_surface._generation_thread.join(timeout=1)
    assert space_surface._generation_thread.is_alive() is False
