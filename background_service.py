import asyncio
import os
import socket
import uuid

from redis import Redis

from config import settings
from routers import generate, order, ledger


LOCK_KEY = os.getenv("BACKGROUND_LOCK_KEY", "ed:background:singleton")
LOCK_TTL_SECONDS = int(os.getenv("BACKGROUND_LOCK_TTL_SECONDS", "60"))
LOCK_RENEW_SECONDS = int(os.getenv("BACKGROUND_LOCK_RENEW_SECONDS", "20"))
LOCK_RETRY_SECONDS = int(os.getenv("BACKGROUND_LOCK_RETRY_SECONDS", "10"))

RENEW_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_LOCK_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def get_redis_conn() -> Redis:
    return Redis.from_url(
        settings.REDIS_URL,
        health_check_interval=20,
        socket_timeout=12,
        socket_connect_timeout=12,
        retry_on_timeout=True,
    )


def acquire_lock(redis_conn: Redis, token: str) -> bool:
    return bool(redis_conn.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL_SECONDS))


def renew_lock(redis_conn: Redis, token: str) -> bool:
    return bool(redis_conn.eval(RENEW_LOCK_SCRIPT, 1, LOCK_KEY, token, LOCK_TTL_SECONDS))


def release_lock(redis_conn: Redis, token: str) -> None:
    redis_conn.eval(RELEASE_LOCK_SCRIPT, 1, LOCK_KEY, token)


async def keep_lock_renewed(redis_conn: Redis, token: str) -> None:
    while True:
        await asyncio.sleep(LOCK_RENEW_SECONDS)
        renewed = await asyncio.to_thread(renew_lock, redis_conn, token)
        if not renewed:
            raise RuntimeError("Background singleton lock was lost")


async def run_background_tasks() -> None:
    await order.repair_unhandled_orders()

    tasks = [
        asyncio.create_task(generate.start_discovery_cache_job(), name="discovery-cache-job"),
        asyncio.create_task(generate.start_result_listener(), name="generate-result-listener"),
        asyncio.create_task(generate.start_pending_recovery_job(), name="pending-recovery-job"),
        asyncio.create_task(ledger.start_ledger_sync_job(), name="ledger-sync-job"),
    ]

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
            raise RuntimeError(f"Background task exited unexpectedly: {task.get_name()}")
        for task in pending:
            task.cancel()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_with_lock(redis_conn: Redis, token: str) -> None:
    renew_task = asyncio.create_task(keep_lock_renewed(redis_conn, token), name="lock-renewer")
    worker_task = asyncio.create_task(run_background_tasks(), name="background-tasks")

    try:
        done, pending = await asyncio.wait(
            [renew_task, worker_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            exc = task.exception()
            if exc:
                raise exc
            raise RuntimeError(f"Background supervisor task exited unexpectedly: {task.get_name()}")
    finally:
        for task in [renew_task, worker_task]:
            task.cancel()
        await asyncio.gather(renew_task, worker_task, return_exceptions=True)


async def main() -> None:
    token = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
    redis_conn = get_redis_conn()

    while True:
        try:
            acquired = await asyncio.to_thread(acquire_lock, redis_conn, token)
            if not acquired:
                print(
                    f"[*] Background singleton lock is held by another instance. "
                    f"Retrying in {LOCK_RETRY_SECONDS}s."
                )
                await asyncio.sleep(LOCK_RETRY_SECONDS)
                continue

            print(f"[*] Acquired background singleton lock: {LOCK_KEY}")
            await run_with_lock(redis_conn, token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[!] Background service loop error: {exc}")
            await asyncio.sleep(LOCK_RETRY_SECONDS)
        finally:
            try:
                await asyncio.to_thread(release_lock, redis_conn, token)
            except Exception as exc:
                print(f"[!] Failed to release background singleton lock: {exc}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Background service stopped.")
