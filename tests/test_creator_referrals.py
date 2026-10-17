from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from pathlib import Path
import importlib.util

import jwt
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import auth
import models
from config import settings
from main import app
from creator_referrals import bind_referral, visit_receipt
from test_order import mock_auth, quantity_order

pytestmark = pytest.mark.usefixtures("mock_auth")


@pytest.fixture
def codes(db):
    db.add_all([models.User(id="creator-a", email="alpha@example.test", is_creator=True), models.User(id="creator-b", email="beta@example.test", is_creator=True)]); db.flush()
    a = models.CreatorCode(code="ALPHA", creator_user_id="creator-a", discount_bps=1000, commission_bps=1500)
    b = models.CreatorCode(code="BETA", creator_user_id="creator-b", discount_bps=2000, commission_bps=1000)
    db.add_all([a, b]); db.commit()
    return a, b


def bind(client, code):
    response = client.post("/api/creators/visit", json={"code": code})
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert client.post("/api/creators/referral", json={"token": receipt["token"]}).status_code == 200
    return receipt


def create(client, request, **fields):
    response = client.post("/api/orders", json={**request, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_default_and_order_override_preserve_acquisition_and_next_order_default(client, db, codes, quantity_order):
    bind(client, "alpha")
    order = create(client, quantity_order, quantity=2)
    assert order["creator_discount"]["code"] == "ALPHA"
    assert order["price"] == 60 and order["discount_amount"] == 6 and order["total_price"] == 64
    changed = create(client, quantity_order, quantity=2, creator_code="beta")
    assert changed["id"] != order["id"] and changed["total_price"] == 58
    original = client.get(f"/api/orders/{order['id']}").json()
    assert original["creator_discount"]["code"] == "ALPHA" and original["total_price"] == 64
    saved = db.query(models.Order).filter_by(id=changed["id"]).one()
    assert saved.creator_commission == 4.8 and saved.creator_code == "BETA"
    source = db.query(models.UserCreatorReferral).one()
    assert source.original_code == source.current_code == "ALPHA"
    following = create(client, quantity_order, checkout_mode="buy_now")
    assert following["creator_discount"]["code"] == "ALPHA"
    assert "commission_bps" not in changed["creator_discount"]


def test_link_receipts_keep_first_source_but_update_recent_source_without_extending_replays(client, db, codes):
    first = bind(client, "ALPHA")
    original = db.query(models.UserCreatorReferral).one().original_at
    bind(client, "BETA")
    row = db.query(models.UserCreatorReferral).one(); db.refresh(row)
    assert row.original_code == "ALPHA" and row.current_code == "BETA" and row.original_at == original
    expiry = row.current_expires_at
    token = jwt.encode({"aud": "creator-referral", "code": "ALPHA", "iat": int(datetime.now(timezone.utc).timestamp()) - 60,
                        "exp": int(datetime.now(timezone.utc).timestamp()) + 600}, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    assert client.post("/api/creators/referral", json={"token": token}).status_code == 200
    db.refresh(row)
    assert row.current_code == "BETA" and row.current_expires_at == expiry
    assert first["code"] == "ALPHA"


@pytest.mark.parametrize("kind", ["invalid", "expired", "wrong-audience", "overlong-window"])
def test_forged_or_expired_receipts_cannot_assign_a_source(client, db, codes, kind):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"aud": "creator-referral", "code": "ALPHA", "iat": now - 60, "exp": now + 60}
    if kind == "expired": payload["exp"] = now - 1
    if kind == "wrong-audience": payload["aud"] = "access"
    if kind == "overlong-window": payload["exp"] = now + 40 * 86400
    token = "invalid" if kind == "invalid" else jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    assert client.post("/api/creators/referral", json={"token": token}).status_code == 200
    assert db.query(models.UserCreatorReferral).count() == 0


@pytest.mark.parametrize("age_days", [31, 365, 3650])
def test_bound_source_stays_default_years_after_receipt_expiry(client, db, codes, quantity_order, age_days):
    bind(client, "ALPHA")
    row = db.query(models.UserCreatorReferral).one()
    clicked = datetime.now(timezone.utc) - timedelta(days=age_days)
    row.original_at = row.current_clicked_at = clicked
    row.current_expires_at = clicked + timedelta(days=30)
    db.commit(); db.refresh(row)
    original, receipt_expiry = row.original_at, row.current_expires_at
    # Returning buyers no longer have the anonymous receipt in browser storage.
    quote = client.post("/api/creators/quote", json={"model_type": "Cute DIY Kit", "quantity": 1})
    assert quote.status_code == 200, quote.text
    assert quote.json()["offer"]["code"] == "ALPHA" and quote.json()["total_price"] == 37
    order = create(client, quantity_order)
    assert order["creator_discount"]["code"] == "ALPHA" and order["total_price"] == 37
    removed = create(client, quantity_order, creator_code="")
    assert removed["creator_discount"] is None and removed["total_price"] == 40
    following = create(client, quantity_order, checkout_mode="buy_now")
    assert following["creator_discount"]["code"] == "ALPHA"
    db.refresh(row)
    assert row.original_code == row.current_code == "ALPHA"
    assert row.original_at == original and row.current_expires_at == receipt_expiry


@pytest.mark.parametrize("case", ["disabled", "expired"])
def test_long_term_default_requires_an_active_unexpired_code(client, db, codes, quantity_order, case):
    bind(client, "ALPHA")
    row = db.query(models.UserCreatorReferral).one()
    row.current_expires_at = datetime.now(timezone.utc) - timedelta(days=365)
    if case == "disabled": codes[0].active = False
    else: codes[0].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    quote = client.post("/api/creators/quote", json={"model_type": "Cute DIY Kit", "quantity": 1})
    assert quote.status_code == 200 and quote.json()["offer"] is None
    order = create(client, quantity_order)
    assert order["creator_discount"] is None and order["total_price"] == 40
    db.refresh(row)
    assert row.original_code == row.current_code == "ALPHA"
    codes[0].active = True; codes[0].expires_at = None; db.commit()
    following = create(client, quantity_order)
    assert following["creator_discount"]["code"] == "ALPHA"


@pytest.mark.parametrize("valid_receipt", [True, False])
def test_google_signup_binds_valid_referral_without_blocking_login_on_invalid_receipt(client, db, codes, monkeypatch, valid_receipt):
    monkeypatch.setattr(auth, "verify_google_token", lambda token: {
        "email": "creator-signup@example.test", "sub": "google-creator-signup",
        "email_verified": True, "name": "New customer",
    })
    monkeypatch.setattr("backend_utils.award_daily_login_credits", lambda *args: None)
    token = visit_receipt(codes[0])["token"] if valid_receipt else "invalid-receipt"
    response = client.post("/api/auth/google", json={"token": "google-credential", "referral_token": token})
    assert response.status_code == 200, response.text
    user_id = response.json()["user"]["id"]
    saved = db.query(models.UserCreatorReferral).filter_by(user_id=user_id).first()
    if valid_receipt:
        assert saved.original_code == saved.current_code == "ALPHA"
    else:
        assert saved is None
    assert db.query(models.AuthSession).filter_by(user_id=user_id).count() == 1


def test_referral_receipt_cannot_authenticate_as_an_access_token(client, codes):
    app.dependency_overrides.pop(auth.get_current_user, None)
    token = visit_receipt(codes[0])["token"]
    response = client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.parametrize("prefix", ["/api", "/skin/api"])
@pytest.mark.parametrize("original_code", ["ALPHA", ""])
@pytest.mark.parametrize("requested_code", ["BETA", "", "ALPHA", "MISSING"])
def test_cart_code_cannot_be_added_replaced_or_removed(client, db, codes, quantity_order, prefix, original_code, requested_code):
    order = create(client, quantity_order, creator_code=original_code)
    assert order["creator_code_locked"] is True
    saved = db.query(models.Order).filter_by(id=order["id"]).one()
    before = (saved.creator_code, saved.creator_user_id, saved.creator_snapshot.copy() if saved.creator_snapshot else None,
              saved.price, saved.total_price, saved.discount_amount, saved.creator_commission, saved.creator_commission_cents)
    response = client.patch(f"{prefix}/orders/{order['id']}/creator-code", json={"code": requested_code, "expected_code": original_code or None})
    assert response.status_code == 409 and response.json()["detail"] == "creator_code_locked"
    db.refresh(saved)
    after = (saved.creator_code, saved.creator_user_id, saved.creator_snapshot, saved.price, saved.total_price,
             saved.discount_amount, saved.creator_commission, saved.creator_commission_cents)
    assert after == before
    unchanged = client.get(f"/api/orders/{order['id']}").json()
    assert unchanged["creator_discount"] == order["creator_discount"] and unchanged["total_price"] == order["total_price"]


def test_quotes_defaults_and_forged_prices_are_computed_by_server(client, db, codes, quantity_order):
    receipt = client.post("/api/creators/visit", json={"code": "ALPHA"}).json()
    quote = client.post("/api/creators/quote", json={"model_type": "Cute DIY Kit", "quantity": 3, "referral_token": receipt["token"]}).json()
    assert quote["offer"]["code"] == "ALPHA" and quote["total_price"] == 91 and quote["discount_amount"] == 9
    order = create(client, quantity_order, quantity=3, creator_code="BETA", creator_offer_version=1,
                   total_price=1, discount_amount=89, creator_commission=10000, creator_snapshot={"code": "ALPHA"})
    assert order["total_price"] == 82 and order["discount_amount"] == 18
    stored = db.query(models.Order).filter_by(id=order["id"]).one()
    assert stored.creator_commission == 7.2


def test_code_change_races_and_wrong_owner_cannot_overwrite_order(client, db, codes, quantity_order):
    order = create(client, quantity_order, creator_code="ALPHA")
    assert client.patch(f"/api/orders/{order['id']}/creator-code", json={"code": "BETA", "expected_code": None}).status_code == 409
    user = models.User(id="other", email="other@example.test"); db.add(user); db.commit()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    assert client.patch(f"/api/orders/{order['id']}/creator-code", json={"code": "BETA", "expected_code": "ALPHA"}).status_code == 404


@pytest.mark.parametrize("state", ["pending_payment", "paid", "shipping", "completed", "refund_pending", "refunded", "voucher", "capture"])
def test_payment_and_fulfillment_lock_discount_and_commission(client, db, codes, quantity_order, state):
    order = create(client, quantity_order, creator_code="ALPHA")
    saved = db.query(models.Order).filter_by(id=order["id"]).one()
    if state == "voucher": saved.paypal_order_id = "PAYPAL-LOCKED"
    elif state == "capture": saved.capture_started = True
    else: saved.status = state
    db.commit()
    result = client.patch(f"/api/orders/{order['id']}/creator-code", json={"code": "BETA", "expected_code": "ALPHA"})
    assert result.status_code == 409 and result.json()["detail"] == "creator_code_locked"
    db.refresh(saved)
    assert saved.creator_code == "ALPHA" and saved.total_price == 37 and saved.creator_commission == 4.05


def test_coupon_pricing_survives_cart_merge_item_removal_and_paypal(client, db, codes, quantity_order, monkeypatch):
    order = create(client, quantity_order, creator_code="ALPHA", quantity=2)
    assert order["creator_code_locked"] is True
    merged = create(client, quantity_order, creator_code="ALPHA")
    assert merged["id"] == order["id"] and merged["total_price"] == 91
    separate = create(client, quantity_order, creator_code="BETA")
    assert separate["id"] != order["id"]
    assert client.delete(f"/api/orders/items/{merged['items'][0]['id']}").status_code == 200
    reduced = client.get(f"/api/orders/{order['id']}").json()
    assert reduced["price"] == 60 and reduced["total_price"] == 64 and reduced["discount_amount"] == 6
    checkout = Mock(return_value={"id": "CREATOR-PAYPAL", "status": "CREATED"})
    monkeypatch.setattr("routers.order.create_paypal_order_api", checkout)
    assert client.post(f"/api/orders/{order['id']}/create-paypal-order").status_code == 200
    assert checkout.call_args.args == (64, order["id"])
    assert client.delete(f"/api/orders/items/{reduced['items'][0]['id']}").status_code == 409
    assert client.get(f"/api/orders/{order['id']}").json()["creator_code_locked"] is True


def test_code_removal_is_rejected_and_catalog_changes_leave_purchase_snapshots_intact(client, db, codes, quantity_order):
    order = create(client, quantity_order, creator_code="ALPHA")
    codes[0].discount_bps = 5000; codes[0].version = 2; db.commit()
    assert client.get(f"/api/orders/{order['id']}").json()["total_price"] == 37
    assert client.post("/api/orders", json={**quantity_order, "creator_code": "ALPHA", "creator_offer_version": 1}).status_code == 409
    cleared = client.patch(f"/api/orders/{order['id']}/creator-code", json={"code": "", "expected_code": "ALPHA"})
    assert cleared.status_code == 409 and cleared.json()["detail"] == "creator_code_locked"
    unchanged = client.get(f"/api/orders/{order['id']}").json()
    assert unchanged["creator_discount"] == order["creator_discount"] and unchanged["total_price"] == 37
    without_code = create(client, quantity_order, creator_code="")
    assert without_code["id"] != order["id"] and without_code["creator_discount"] is None and without_code["total_price"] == 40


def test_management_requires_admin_and_excludes_refunds_from_commissions(client, db, codes, quantity_order, monkeypatch):
    assert client.get("/api/creators/manage").status_code == 403
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "test_order@example.com")
    for status, refund in [("completed", None), ("paid", None), ("refunded", "completed"), ("refund_pending", "pending")]:
        order = create(client, quantity_order, creator_code="ALPHA", checkout_mode="buy_now")
        saved = db.query(models.Order).filter_by(id=order["id"]).one()
        saved.status = status; saved.refund_status = refund; saved.paid_at = datetime.now(timezone.utc) - timedelta(days=3); db.commit()
    record = next(row for row in client.get("/api/creators/manage").json() if row["code"] == "ALPHA")
    assert record["paid_sales"] == 54 and record["eligible_commission"] == record["pending_commission"] == 4.05
    update = {"creator_email": "alpha@example.test", "discount_bps": 1000, "commission_bps": 1200, "expected_version": 1}
    assert client.put("/api/creators/manage/ALPHA", json=update).json()["version"] == 2
    assert client.put("/api/creators/manage/ALPHA", json=update).status_code == 409
    assert client.put("/api/creators/manage/NEW", json={**update, "expected_version": None, "discount_bps": 10000}).status_code == 422


def test_creator_migration_preserves_existing_totals(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/f4a92b6d013e_add_creator_referrals.py"
    spec = importlib.util.spec_from_file_location("creator_migration", path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE users (id VARCHAR(16) PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE orders (id VARCHAR(16), total_price FLOAT)"))
        connection.execute(sa.text("INSERT INTO orders VALUES ('existing', 40)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert tuple(connection.execute(sa.text("SELECT total_price, discount_amount, creator_commission, creator_code FROM orders")).one()) == (40, 0, 0, None)
        migration.downgrade()
        assert "creator_code" not in {column["name"] for column in sa.inspect(connection).get_columns("orders")}
    engine.dispose()
