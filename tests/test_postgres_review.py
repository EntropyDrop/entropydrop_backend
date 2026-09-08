"""Opt-in PostgreSQL migration and race tests, isolated in random databases.

Set REVIEW_POSTGRES_URL to a disposable database, then run this module.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from fastapi import HTTPException

import backend_utils
import models
from database import Base
from order_inventory import lock_order
from routers import order as orders

pytestmark = pytest.mark.skipif(not os.environ.get("REVIEW_POSTGRES_URL"), reason="Requires disposable PostgreSQL")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def postgres():
    url = sa.engine.make_url(os.environ["REVIEW_POSTGRES_URL"])
    admin = sa.create_engine(url, isolation_level="AUTOCOMMIT")
    database_name = "ed_review_" + uuid4().hex
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
    isolated_url = url.set(database=database_name)
    engine = sa.create_engine(isolated_url)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        yield engine, isolated_url
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


def migrate(url, action, revision):
    env = {**os.environ, "ENV_FILE": "/dev/null", "DATABASE_URL": url.render_as_string(hide_password=False), "SPACE_STANDALONE": "false"}
    result = subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


@pytest.mark.parametrize("legacy", [False, True])
def test_empty_and_legacy_database_upgrade_to_head(postgres, legacy):
    engine, url = postgres
    if legacy:
        spec = importlib.util.spec_from_file_location("baseline", ROOT / "alembic/versions/40d182b3a053_initial_schema.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module._historical_metadata().create_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, email, username) VALUES ('legacy-user', 'legacy@example.test', 'Existing user')"))
    migrate(url, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == "a8e6c4d20918"
        if legacy:
            assert conn.execute(sa.text("SELECT username FROM users WHERE id = 'legacy-user'")).scalar_one() == "Existing user"
    inspector = sa.inspect(engine)
    # Finding 8 is explicitly excluded: do not silently change the v5 constraint.
    constraints = inspector.get_check_constraints("space_market_resources")
    assert any("schema_version = 5" in c["sqltext"] for c in constraints)
    for name, table in Base.metadata.tables.items():
        if name.startswith("space_"):
            continue
        actual = {column["name"] for column in inspector.get_columns(name)}
        assert actual == set(table.columns.keys()), name


def test_upgrade_backfills_addresses_for_existing_orders(postgres):
    engine, url = postgres
    migrate(url, "upgrade", "e3a97d50c812")
    metadata = sa.MetaData()
    addresses = sa.Table("shipping_addresses", metadata, autoload_with=engine)
    order_table = sa.Table("orders", metadata, autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(addresses.insert().values(id="addr-old", user_id="old-user", country="US", phone="123", zip_code="12345", state="CA", city="City", detail_address="Historical address", is_default=True))
        conn.execute(order_table.insert().values(id="order-old", user_id="old-user", address_id="addr-old", order_type="print", status="paid", price=20, shipping_fee=0, total_price=20))
    migrate(url, "upgrade", "head")
    with Session(engine) as db:
        order = db.get(models.Order, "order-old")
        assert order.address_snapshot["detail_address"] == "Historical address"
        db.delete(db.get(models.ShippingAddress, "addr-old"))
        db.commit()
        assert order.address_snapshot["country"] == "US"


def seed_orders(engine, count):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(models.User(id="race-user", email="race@example.test", credits=0))
        db.add(models.ModelSalesLimit(model_type="race-model", order_type="print", stock=1, price=20))
        for i in range(count):
            db.add(models.Order(id=f"race-{i}", user_id="race-user", order_type="print", price=20, total_price=20, paypal_order_id=f"PAY-{i}"))
            db.add(models.OrderItem(order_id=f"race-{i}", model_type="race-model", price=20))
        db.commit()


@pytest.mark.parametrize("same_order", [False, True])
def test_concurrent_payments_never_oversell_or_capture_twice(postgres, monkeypatch, same_order):
    engine, _ = postgres
    seed_orders(engine, 1 if same_order else 2)
    captures = []
    def payload(paypal_id, status):
        return {"status": status, "purchase_units": [{"custom_id": "race-" + paypal_id.split("-")[1], "amount": {"currency_code": "USD", "value": "20"}}]}
    monkeypatch.setattr(orders, "get_paypal_order_api", lambda p: payload(p, "APPROVED"))
    def capture(paypal_id):
        captures.append(paypal_id)
        return payload(paypal_id, "COMPLETED")
    monkeypatch.setattr(orders, "capture_paypal_order_api", capture)
    barrier = threading.Barrier(2)
    def pay(i):
        with Session(engine) as db:
            barrier.wait(timeout=5)
            try:
                order = lock_order(db, f"race-{i}")
                orders.complete_order_payment(db, order, order.paypal_order_id)
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pay, [0, 0 if same_order else 1]))
    assert sorted(results) == ([200, 200] if same_order else [200, 409])
    assert len(captures) == 1
    with Session(engine) as db:
        assert db.query(models.ModelSalesLimit).one().stock == 0
        assert db.query(models.Order).filter_by(status="paid").count() == 1


def test_concurrent_subscription_grants_share_database_idempotency(postgres):
    engine, _ = postgres
    seed_orders(engine, 0)
    barrier = threading.Barrier(2)
    paid_at = datetime.now(timezone.utc) - timedelta(days=4)
    def grant(_):
        with Session(engine) as db:
            user = db.get(models.User, "race-user")
            barrier.wait(timeout=5)
            backend_utils.award_subscription_credits(db, user, "pro-plus", "SUB-RACE", False, paid_at=paid_at)
            db.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(grant, range(2)))
    with Session(engine) as db:
        assert db.get(models.User, "race-user").credits == 80
        assert db.query(models.CreditLog).count() == 1
