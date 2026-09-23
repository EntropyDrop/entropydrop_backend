"""Use a private Redis process to exercise real RQ/Lua concurrency semantics."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest
from redis import Redis, ConnectionError
from rq import Queue, Retry
from rq.job import Job

import auth
import generation_priority as priority
import models
from main import app
from routers import generate, order


@pytest.fixture(scope="module")
def redis_server():
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("redis-server is required for RQ promotion integration tests")
    # macOS limits Unix socket paths to 104 bytes.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pro-rq-", dir="/tmp") as directory:
        socket = str(Path(directory) / "redis.sock")
        process = subprocess.Popen(
            [executable, "--port", "0", "--unixsocket", socket, "--save", "", "--appendonly", "no"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        connection = Redis(unix_socket_path=socket)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail(process.stdout.read().decode())
                try:
                    if connection.ping():
                        break
                except ConnectionError:
                    time.sleep(0.02)
            else:
                pytest.fail("Private Redis did not start")
            yield connection
        finally:
            connection.close()
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()


@pytest.fixture
def redis_conn(redis_server, monkeypatch):
    redis_server.flushdb()
    monkeypatch.setattr(priority, "redis_conn", redis_server)
    monkeypatch.setattr(generate, "redis_conn", redis_server)
    return redis_server


def queued(connection, log_id, stage="text_to_image", high=False):
    queue = Queue(("high_" if high else "") + priority.STAGE_QUEUES[stage], connection=connection)
    return queue.enqueue(
        "worker_tasks.task_text_to_image", args=(log_id, True, "prompt"),
        job_id=f"generation_{log_id}_{stage}", retry=Retry(max=5, interval=30),
        job_timeout=400,
    )


def test_move_preserves_job_and_appends_once(redis_conn):
    existing = queued(redis_conn, "existing", high=True)
    job = queued(redis_conn, "skin")
    original = redis_conn.hgetall(job.key)
    for _ in range(3):
        priority.promote_job(redis_conn, "skin", job.id, "queue_text_to_image")
    assert redis_conn.lrange("rq:queue:queue_text_to_image", 0, -1) == []
    assert redis_conn.lrange("rq:queue:high_queue_text_to_image", 0, -1) == [existing.id.encode(), job.id.encode()]
    assert redis_conn.hgetall(job.key) == {**original, b"origin": b"high_queue_text_to_image"}
    assert Job.fetch(job.id, connection=redis_conn).args == job.args
    assert redis_conn.sismember("rq:queues", "rq:queue:high_queue_text_to_image")


@pytest.mark.parametrize("intermediate", [False, True])
def test_dequeued_job_is_not_recreated_even_before_started(redis_conn, intermediate):
    job = queued(redis_conn, "taken")
    source = "rq:queue:queue_text_to_image"
    if intermediate:
        redis_conn.lmove(source, source + ":intermediate")
    else:
        redis_conn.lpop(source)
    assert job.get_status(refresh=True) == "queued"
    assert priority.promote_job(redis_conn, "taken", job.id, "queue_text_to_image") == 0
    assert redis_conn.llen("rq:queue:high_queue_text_to_image") == 0
    assert redis_conn.hget(job.key, "origin") == b"queue_text_to_image"


@pytest.mark.parametrize("status", ["started", "scheduled", "deferred", "finished", "failed", "canceled", "stopped"])
def test_nonqueued_jobs_are_not_moved(redis_conn, status):
    job = queued(redis_conn, "other-state")
    redis_conn.hset(job.key, "status", status)
    assert priority.promote_job(redis_conn, "other-state", job.id, "queue_text_to_image") == 0
    assert redis_conn.lrange("rq:queue:queue_text_to_image", 0, -1) == [job.id.encode()]


def test_cancelled_generation_is_not_promoted(redis_conn):
    job = queued(redis_conn, "cancelled")
    redis_conn.set("generation:cancelled:cancelled", "1")
    assert priority.promote_job(redis_conn, "cancelled", job.id, "queue_text_to_image") == 0
    assert redis_conn.hget(job.key, "origin") == b"queue_text_to_image"


def test_concurrent_pop_and_promotion_produce_one_delivery(redis_conn):
    with ThreadPoolExecutor(max_workers=2) as pool:
        for i in range(60):
            log_id = f"race-{i}"
            job = queued(redis_conn, log_id)
            barrier = threading.Barrier(2)

            def promote():
                barrier.wait(timeout=3)
                return priority.promote_job(redis_conn, log_id, job.id, "queue_text_to_image")

            def consume():
                barrier.wait(timeout=3)
                return redis_conn.blpop(["rq:queue:high_queue_text_to_image", "rq:queue:queue_text_to_image"], timeout=1)

            move, pop = pool.submit(promote), pool.submit(consume)
            move.result(timeout=3)
            key, received = pop.result(timeout=3)
            assert received == job.id.encode()
            assert redis_conn.hget(job.key, "origin") == key.removeprefix(b"rq:queue:")
            assert redis_conn.llen("rq:queue:queue_text_to_image") == 0
            assert redis_conn.llen("rq:queue:high_queue_text_to_image") == 0


@pytest.fixture
def subscriber(db, client, monkeypatch):
    user = models.User(id="priority-owner", email="priority@example.test", pro_level="free", credits=12)
    db.add(user)
    db.commit()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    monkeypatch.setattr(order.settings, "PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PRIORITY")
    monkeypatch.setattr(order.settings, "PAYPAL_WEBHOOK_ID", "WEBHOOK-PRIORITY")
    paid_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    subscription = {
        "status": "ACTIVE", "plan_id": "PLAN-PRIORITY", "custom_id": user.id,
        "billing_info": {"last_payment": {"time": paid_at.isoformat(), "amount": {"value": "20", "currency_code": "USD"}},
                         "next_billing_time": (paid_at + timedelta(days=30)).isoformat()},
    }
    monkeypatch.setattr(order, "get_paypal_subscription_api", lambda _: subscription)
    monkeypatch.setattr("payment_utils.get_paypal_subscription_api", lambda _: subscription)
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda *args: True)
    return user, subscription


def generation(db, user, **kwargs):
    values = {"id": "priority-skin", "user_id": user.id, "mode": "aigc_text_to_skin", "status": "pending",
              "is_pro": False, "license": "cc-by-nc-4.0", "credits_charged": 3, "is_public": True}
    values.update(kwargs)
    log = models.GenerationLog(**values)
    db.add(log)
    db.commit()
    return log


def activate(client):
    return client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-PRIORITY"})


def sale(client):
    return client.post("/skin/api/webhooks/paypal", json={"event_type": "PAYMENT.SALE.COMPLETED", "resource": {
        "id": "SALE-PRIORITY", "billing_agreement_id": "SUB-PRIORITY", "amount": {"total": "20", "currency": "USD"},
    }})


@pytest.mark.parametrize("webhook_first", [False, True])
def test_paid_subscription_upgrades_queue_without_changing_rights_or_charges(client, db, subscriber, redis_conn, webhook_first):
    user, _ = subscriber
    log = generation(db, user)
    granted_at = log.license_granted_at
    job = queued(redis_conn, log.id)
    for request in ([sale, activate, sale, activate] if webhook_first else [activate, sale, activate, sale]):
        assert request(client).status_code == 200
    db.refresh(log)
    db.refresh(user)
    assert log.pro_priority is True
    assert log.is_pro is False and log.license == "cc-by-nc-4.0"
    assert log.license_granted_at == granted_at
    assert log.credits_charged == 3 and not log.credits_refunded
    assert user.credits == 92
    assert db.query(models.CreditLog).count() == 1
    assert redis_conn.lrange("rq:queue:high_queue_text_to_image", 0, -1) == [job.id.encode()]
    history = client.get("/skin/api/history").json()
    assert history["items"][0]["pro_priority"] is True
    assert history["items"][0]["is_pro"] is False


@pytest.mark.parametrize("unpaid", [True, False])
def test_unpaid_or_foreign_subscription_cannot_promote(client, db, subscriber, redis_conn, unpaid):
    user, subscription = subscriber
    log = generation(db, user)
    job = queued(redis_conn, log.id)
    if unpaid:
        subscription["billing_info"].pop("last_payment")
    else:
        subscription["custom_id"] = "another-user"
    assert activate(client).status_code == (409 if unpaid else 403)
    db.refresh(log)
    assert not log.pro_priority
    assert redis_conn.lrange("rq:queue:queue_text_to_image", 0, -1) == [job.id.encode()]
    assert not redis_conn.exists(f"generation:pro_priority:{log.id}")


def test_grant_excludes_terminal_deleted_withdrawn_and_foreign_logs(client, db, subscriber, redis_conn):
    user, _ = subscriber
    untouched = [
        generation(db, user, id="done", status="success"),
        generation(db, user, id="failed", status="failed"),
        generation(db, user, id="deleted", is_deleted=True),
        generation(db, user, id="withdrawing", withdrawal={"action": "delete"}),
        generation(db, user, id="foreign", user_id="someone-else"),
    ]
    running = generation(db, user, id="running", status="processing")
    job = queued(redis_conn, running.id)
    redis_conn.lpop("rq:queue:queue_text_to_image")
    assert activate(client).status_code == 200
    for log in untouched:
        db.refresh(log)
        assert not log.pro_priority
        assert not redis_conn.exists(f"generation:pro_priority:{log.id}")
    db.refresh(running)
    assert running.pro_priority  # The next stage is eligible.
    assert redis_conn.hget(job.key, "origin") == b"queue_text_to_image"
    assert redis_conn.llen("rq:queue:high_queue_text_to_image") == 0


def test_redis_outage_does_not_fail_payment_and_reconciliation_repairs_it(client, db, subscriber, redis_conn, monkeypatch):
    from conftest import TestingSessionLocal
    user, _ = subscriber
    log = generation(db, user)
    job = queued(redis_conn, log.id)
    with monkeypatch.context() as patch:
        patch.setattr(redis_conn, "set", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("unavailable")))
        assert activate(client).status_code == 200
    db.refresh(log)
    assert log.pro_priority
    assert redis_conn.hget(job.key, "origin") == b"queue_text_to_image"
    monkeypatch.setattr(priority, "SessionLocal", TestingSessionLocal)
    priority.reconcile_pro_priority()
    assert redis_conn.hget(job.key, "origin") == b"high_queue_text_to_image"
    # An upgraded task keeps its priority across recovery and subscription expiry.
    user.pro_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    redis_conn.delete(f"generation:pro_priority:{log.id}")
    priority.reconcile_pro_priority()
    assert redis_conn.get(f"generation:pro_priority:{log.id}") == b"1"
    assert generate.generation_queue_prefix(log, False) == "high_"


def test_reconcile_covers_preexisting_subscription_and_late_enqueue(db, subscriber, redis_conn, monkeypatch):
    from conftest import TestingSessionLocal
    user, _ = subscriber
    user.pro_level = "pro-plus"
    user.pro_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    log = generation(db, user)
    monkeypatch.setattr(priority, "SessionLocal", TestingSessionLocal)
    priority.reconcile_pro_priority()  # No RQ job yet.
    job = queued(redis_conn, log.id)
    priority.reconcile_pro_priority()
    db.refresh(log)
    assert log.pro_priority
    assert redis_conn.hget(job.key, "origin") == b"high_queue_text_to_image"


def test_poll_and_retry_keep_schedule_and_promote_when_due(db, subscriber, redis_conn):
    user, _ = subscriber
    log = generation(db, user, pro_priority=True, status="processing")
    queue = Queue("queue_real_to_render", connection=redis_conn)
    delayed = queue.enqueue_in(timedelta(seconds=60), "tasks.poll_real_to_render", args=(log.id,),
                               job_id=f"real_to_render_poll_{log.id}_1")
    retry = queue.enqueue_in(timedelta(seconds=30), "tasks.submit_real_to_render", args=(log.id,),
                             job_id=f"generation_{log.id}_real_to_render")
    before = redis_conn.zrange(queue.scheduled_job_registry.key, 0, -1, withscores=True)
    priority.sync_pro_priority(db)
    assert redis_conn.zrange(queue.scheduled_job_registry.key, 0, -1, withscores=True) == before
    assert delayed.get_status(refresh=True) == "scheduled"
    # Emulate RQ scheduling the same existing jobs when their delay elapses.
    for job in (delayed, retry):
        queue.scheduled_job_registry.remove(job)
        queue.enqueue_job(job)
    priority.sync_pro_priority(db)
    assert set(redis_conn.lrange("rq:queue:high_queue_real_to_render", 0, -1)) == {delayed.id.encode(), retry.id.encode()}


def test_queue_position_uses_actual_priority_and_migration_tail(db, subscriber, redis_conn):
    user, _ = subscriber
    log = generation(db, user, pro_priority=True)
    queued(redis_conn, "normal-ahead")
    queued(redis_conn, log.id)
    queued(redis_conn, "pro-ahead", high=True)
    assert generate.get_queue_position(db, log.id) == 2
    priority.sync_pro_priority(db)
    assert generate.get_queue_position(db, log.id) == 1
    redis_conn.lpop("rq:queue:high_queue_text_to_image")
    assert generate.get_queue_position(db, log.id) == 0


def test_subscription_order_grants_priority_in_same_transaction(db, subscriber):
    user, _ = subscriber
    log = generation(db, user)
    payment = models.Order(id="one-off", user_id=user.id, order_type="subscription", price=20, total_price=20)
    db.add_all([payment, models.OrderItem(order_id=payment.id, model_type="pro-plus", price=20)])
    db.commit()
    order._activate_order_benefits(payment, db, user)
    db.flush()
    assert log.pro_priority
    db.rollback()
    db.refresh(log)
    assert not log.pro_priority


def test_migration_backfills_only_priority_and_preserves_snapshots():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / "alembic/versions/c82e7a4d901b_add_generation_pro_priority.py"
    spec = importlib.util.spec_from_file_location("priority_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with sa.create_engine("sqlite://").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE generation_logs (id text, is_pro boolean, license text)")
        connection.exec_driver_sql("INSERT INTO generation_logs VALUES ('free', false, 'cc-by-nc-4.0'), ('pro', true, 'commercial')")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.exec_driver_sql("SELECT id, is_pro, license, pro_priority FROM generation_logs ORDER BY id").all() == [
                ("free", 0, "cc-by-nc-4.0", 0), ("pro", 1, "commercial", 1),
            ]
            migration.downgrade()
