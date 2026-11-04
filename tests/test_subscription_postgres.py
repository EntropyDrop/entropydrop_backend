"""Subscription migration and real row-lock races on disposable PostgreSQL databases."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import models
import payment_utils
import subscription_lifecycle as lifecycle
from database import Base
from test_postgres_review import postgres, migrate

pytestmark = pytest.mark.skipif(not os.environ.get("REVIEW_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


def test_subscription_migration_backfills_without_assuming_cancellation_was_confirmed(postgres):
    engine, url = postgres
    migrate(url, "upgrade", "d6a28f7c913e")
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id,email,paypal_subscription_id,paypal_subscription_status) VALUES ('active-user','active@example.test','SUB-A','ACTIVE'), ('closed-user','closed@example.test','SUB-C','CANCELLED')"))
    migrate(url, "upgrade", "head")
    with Session(engine) as db:
        rows = {row.paypal_id:row for row in db.query(models.PaypalSubscription).all()}
        assert set(rows) == {"SUB-A", "SUB-C"}
        assert rows["SUB-A"].status == "ACTIVE"
        assert rows["SUB-C"].status == "CANCELLED"
        assert rows["SUB-C"].cancelled_at is None
        assert rows["SUB-C"].cancel_requested_at is None
    migrate(url, "downgrade", "d6a28f7c913e")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM users WHERE paypal_subscription_id IS NOT NULL")).scalar_one() == 2
    migrate(url, "upgrade", "head")


def seed(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.User(id="race-user", email="race@example.test", credits=0))
        db.commit()


def test_concurrent_checkouts_create_only_one_paypal_contract(postgres, monkeypatch):
    engine, _ = postgres
    seed(engine)
    calls = []
    remote = {"id":"SUB-ONE","custom_id":"race-user","plan_id":"PLAN-PLUS","status":"APPROVAL_PENDING",
        "links":[{"rel":"approve","href":"https://www.paypal.com/approve"}]}
    def create(**kwargs):
        calls.append(kwargs["request_id"])
        return deepcopy(remote)
    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", create)
    monkeypatch.setattr(payment_utils, "get_paypal_subscription_api", lambda _:deepcopy(remote))
    barrier = threading.Barrier(2)
    def checkout(_):
        with Session(engine) as db:
            barrier.wait(timeout=5)
            return lifecycle.create_checkout(db, "race-user", "PLAN-PLUS", "https://entropydrop.com/credits")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(checkout, range(2)))
    assert results[0] == results[1]
    assert len(calls) == 1
    with Session(engine) as db:
        assert db.query(models.PaypalSubscription).count() == 1


def test_cancellation_serializes_with_late_activation(postgres, monkeypatch):
    engine, _ = postgres
    seed(engine)
    with Session(engine) as db:
        user = db.get(models.User, "race-user")
        user.paypal_subscription_id, user.paypal_subscription_status = "SUB-RACE", "ACTIVE"
        db.commit()
    remote = {"id":"SUB-RACE","custom_id":"race-user","plan_id":"PLAN-PLUS","status":"ACTIVE",
              "create_time":(lifecycle.now()-timedelta(days=1)).isoformat()}
    entered, activate_started, release = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(payment_utils, "get_paypal_subscription_api", lambda _:deepcopy(remote))
    def cancel(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        remote["status"] = "CANCELLED"
        return True
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", cancel)
    def cancel_request():
        with Session(engine) as db:
            return lifecycle.cancel_all(db, "race-user")
    def activate():
        with Session(engine) as db:
            # Preload the stale ORM user as auth would, then use the shared lock.
            db.get(models.User, "race-user")
            activate_started.set()
            user = lifecycle.lock_user(db, "race-user")
            row = lifecycle.remember(db, user, "SUB-RACE", payment_utils.get_paypal_subscription_api("SUB-RACE"))
            result = lifecycle.select_contract(db, user, row, explicit=True)
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(cancel_request)
        assert entered.wait(timeout=5)
        activated = pool.submit(activate)
        assert activate_started.wait(timeout=5)
        release.set()
        assert cancelled.result(timeout=10)["status"] == "success"
        assert activated.result(timeout=10) is False
    with Session(engine) as db:
        assert db.get(models.User, "race-user").paypal_subscription_status == "CANCELLED"
        assert db.query(models.PaypalSubscription).one().cancelled_at


def test_contract_loaded_before_lock_cannot_overwrite_committed_cancellation(postgres):
    engine, _ = postgres
    seed(engine)
    data = {"id":"SUB-STALE","custom_id":"race-user","status":"ACTIVE"}
    with Session(engine) as db:
        user = lifecycle.lock_user(db, "race-user")
        row = lifecycle.remember(db, user, "SUB-STALE", data)
        lifecycle.select_contract(db, user, row)
        db.commit()
    with Session(engine) as stale, Session(engine) as cancelling:
        old_snapshot = stale.query(models.PaypalSubscription).one()
        assert old_snapshot.cancelled_at is None
        user = lifecycle.lock_user(cancelling, "race-user")
        lifecycle.remember(cancelling, user, "SUB-STALE", {**data,"status":"CANCELLED"})
        cancelling.commit()
        user = lifecycle.lock_user(stale, "race-user")
        row = lifecycle.remember(stale, user, "SUB-STALE", data)
        lifecycle.select_contract(stale, user, row)
        stale.commit()
        assert row.cancelled_at and user.paypal_subscription_status == "CANCELLED"
