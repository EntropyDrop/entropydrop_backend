"""Regression coverage for the September frontend/backend review."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

import auth
import backend_utils
import models
import skin_withdrawal
from main import app
from order_inventory import lock_order, reserve_inventory, expire_inventory_holds
from routers import order as orders


@pytest.fixture
def account(db):
    user = models.User(id="review-owner", email="review@example.test", pro_level="pro-plus",
                       pro_expires_at=datetime.now(timezone.utc) + timedelta(days=30), credits=0)
    db.add(user)
    db.commit()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(auth.get_current_user, None)


def paid_subscription(user, when=None):
    when = when or datetime.now(timezone.utc) - timedelta(days=4)
    return {"status": "ACTIVE", "plan_id": "PLAN-PLUS", "custom_id": user.id,
            "billing_info": {"last_payment": {"time": when.isoformat(), "amount": {"value": "20", "currency_code": "USD"}},
                             "next_billing_time": (when + timedelta(days=30)).isoformat()}}


def test_subscription_repeated_activation_and_webhook_share_cycle(client, db, account, monkeypatch):
    monkeypatch.setattr(orders.settings, "PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PLUS")
    monkeypatch.setattr(orders.settings, "PAYPAL_WEBHOOK_ID", "WEBHOOK")
    payload = paid_subscription(account)
    monkeypatch.setattr(orders, "get_paypal_subscription_api", lambda _: payload)
    monkeypatch.setattr("payment_utils.get_paypal_subscription_api", lambda _: payload)
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda *args: True)
    for _ in range(2):
        assert client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-REVIEW"}).status_code == 200
    grant = db.query(models.CreditLog).filter_by(action="subscription_grant").one()
    grant.created_at = datetime.now(timezone.utc) - timedelta(days=4)
    db.commit()
    event = {"event_type": "PAYMENT.SALE.COMPLETED", "resource": {"id": "SALE-REVIEW", "billing_agreement_id": "SUB-REVIEW", "amount": {"total": "20", "currency": "USD"}}}
    for _ in range(2):
        assert client.post("/skin/api/webhooks/paypal", json=event).status_code == 200
    assert client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-REVIEW"}).status_code == 200
    db.refresh(account)
    assert account.credits == 80
    assert db.query(models.CreditLog).filter_by(action="subscription_grant").count() == 1
    # A distinct verified payment is eligible, even after a long outage.
    payload["billing_info"] = paid_subscription(account, datetime.now(timezone.utc) - timedelta(hours=1))["billing_info"]
    assert client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-REVIEW"}).status_code == 200
    db.refresh(account)
    assert account.credits == 160


def test_activation_requires_payment_and_fallback_expiry_is_fixed(client, db, account, monkeypatch):
    monkeypatch.setattr(orders.settings, "PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PLUS")
    payload = paid_subscription(account)
    payment = payload["billing_info"].pop("last_payment")
    monkeypatch.setattr(orders, "get_paypal_subscription_api", lambda _: payload)
    assert client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-REVIEW"}).status_code == 409
    db.rollback()
    assert account.credits == 0
    assert account.paypal_subscription_id == "SUB-REVIEW"
    payload["billing_info"] = {"last_payment": payment}
    for _ in range(3):
        assert client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-REVIEW"}).status_code == 200
    db.refresh(account)
    assert account.credits == 80
    assert account.pro_expires_at.replace(tzinfo=timezone.utc) == datetime.fromisoformat(payment["time"]) + timedelta(days=34)


def test_historical_subscription_grant_is_adopted_without_new_credits(db, account):
    paid_at = datetime.now(timezone.utc) - timedelta(days=10)
    old = models.CreditLog(user_id=account.id, amount=80, action="subscription_grant", source="Subscription Activation Grant: SUB-OLD", created_at=paid_at + timedelta(minutes=1))
    account.credits = 80
    db.add(old)
    db.commit()
    backend_utils.award_subscription_credits(db, account, "pro-plus", "SUB-OLD", False, paid_at=paid_at)
    db.commit()
    assert account.credits == 80 and old.idempotency_key
    db.add(models.CreditLog(user_id=account.id, amount=80, action="subscription_grant", idempotency_key=old.idempotency_key))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def skin(db, owner, **kwargs):
    is_public = kwargs.pop("is_public", True)
    log = models.GenerationLog(user_id=owner, mode="human_upload", status="success", is_public=is_public,
                               result="generations/review.png", source="uploads/review.png", **kwargs)
    db.add(log)
    db.commit()
    return log


def test_stale_likes_and_private_collection_cannot_sign_foreign_skin(client, db, account, monkeypatch):
    log = skin(db, "other-owner")
    col = models.Collection(user_id=account.id, name="private", is_public=False)
    db.add(col)
    db.flush()
    db.add_all([models.UserLike(user_id=account.id, log_id=log.id), models.CollectionItem(collection_id=col.id, log_id=log.id, type="image", data={"preview": "orders/foreign/item.png"})])
    log.is_public = False
    db.commit()
    signer = MagicMock(return_value="signed")
    monkeypatch.setattr("s3_utils.generate_presigned_url_get", signer)
    for cid in (col.id, "liked"):
        result = client.get("/skin/api/collections/items", params={"collection_id": cid, "user_id": account.id})
        assert result.status_code == 200 and result.json()["total"] == 0
    result = client.get("/skin/api/collections").json()
    assert all(c["item_count"] == 0 and not c["previews"] for c in result["items"])
    liked = next(c for c in result["original_items"] if c["id"] == "liked")
    assert liked["item_count"] == 0 and not liked["previews"]
    signer.assert_not_called()


def test_unbound_collection_data_never_signs_or_echoes_urls(client, db, account, monkeypatch):
    col = models.Collection(user_id=account.id, name="private", is_public=False)
    db.add(col)
    db.commit()
    data = {"result": "orders/foreign/item.png", "preview": "https://foreign.example/secret"}
    response = client.post("/skin/api/collections/items", json={"collection_id": col.id, "name": "forged", "type": "image", "data": data})
    assert response.status_code == 400
    db.add(models.CollectionItem(collection_id=col.id, type="image", data=data))
    db.commit()
    signer = MagicMock()
    monkeypatch.setattr("s3_utils.generate_presigned_url_get", signer)
    response = client.get("/skin/api/collections/items", params={"collection_id": col.id, "user_id": account.id})
    assert response.json()["items"] == []
    signer.assert_not_called()


@pytest.mark.parametrize("is_public", [True, False])
@pytest.mark.parametrize("endpoint", ["make_private", "make_public"])
def test_skin_visibility_conversion_routes_are_absent(client, db, account, withdrawal_storage, is_public, endpoint):
    storage, cdn = withdrawal_storage
    log = skin(db, account.id, is_public=is_public)
    assert client.post(f"/skin/api/logs/{log.id}/{endpoint}").status_code == 404
    db.refresh(log)
    assert log.is_public == is_public
    assert not log.is_deleted and log.withdrawal is None
    assert log.result == "generations/review.png"
    assert storage.mock_calls == [] and cdn.mock_calls == []
    assert all(not path.endswith(f"/{endpoint}") for path in app.openapi()["paths"])


@pytest.mark.parametrize("is_public", [True, False])
def test_skin_rename_cannot_change_visibility(client, db, account, is_public):
    log = skin(db, account.id, is_public=is_public)
    response = client.patch(f"/skin/api/logs/{log.id}/name", json={"name": "Renamed", "is_public": not is_public})
    assert response.status_code == 200
    db.refresh(log)
    assert log.name == "Renamed" and log.is_public == is_public


def test_withdrawal_rejects_legacy_conversion_without_touching_assets(db, account, withdrawal_storage):
    storage, cdn = withdrawal_storage
    log = skin(db, account.id)
    state = {"action": "private", "public": True, "keys": [log.result],
             "copied": [], "deleted": [], "invalidation": None}
    log.withdrawal = state
    db.commit()
    for operation in (skin_withdrawal.begin, skin_withdrawal.resume):
        with pytest.raises(HTTPException) as exc:
            operation(db, log)
        assert exc.value.status_code == 409
        db.refresh(log)
        assert log.is_public and not log.is_deleted and log.withdrawal == state
    assert storage.mock_calls == [] and cdn.mock_calls == []


def test_cdn_pending_and_delete_failures_preserve_manifest(client, db, account, withdrawal_storage):
    storage, cdn = withdrawal_storage
    log = skin(db, account.id)
    def request():
        return client.delete(f"/skin/api/logs/{log.id}")
    storage.delete_object.side_effect = [None, RuntimeError("delete failed")]
    assert request().status_code == 503
    db.refresh(log)
    assert len(log.withdrawal["deleted"]) == 1
    storage.delete_object.side_effect = None
    cdn.get_invalidation.return_value = {"Invalidation": {"Status": "InProgress"}}
    assert request().status_code == 503
    db.refresh(log)
    assert log.withdrawal["invalidation"] == "invalidation-test"
    cdn.get_invalidation.return_value = {"Invalidation": {"Status": "Completed"}}
    assert request().status_code == 200
    assert cdn.create_invalidation.call_count == 1  # Poll, do not recreate forever.
    db.refresh(log)
    assert log.withdrawal is None
    assert log.is_deleted and log.is_public
    storage.copy_object.assert_not_called()


def test_private_skin_deletion_uses_private_storage(client, db, account, withdrawal_storage):
    storage, cdn = withdrawal_storage
    log = skin(db, account.id, is_public=False)
    assert client.delete(f"/skin/api/logs/{log.id}").status_code == 200
    db.refresh(log)
    assert log.is_deleted and not log.is_public and log.withdrawal is None
    assert log.source is None and log.result is None
    assert storage.delete_object.call_count == 2
    assert all(call.kwargs["Bucket"] == skin_withdrawal.settings.AWS_PRIVATE_BUCKET_NAME
               for call in storage.delete_object.call_args_list)
    storage.copy_object.assert_not_called()
    assert cdn.mock_calls == []


def print_order(db, account, stock=1):
    cfg = models.ModelSalesLimit(model_type="review-print", order_type="print", stock=stock, price=20)
    address = models.ShippingAddress(user_id=account.id, country="US", phone="+12345678", zip_code="12345", state="CA", city="City", detail_address="Original address")
    db.add_all([cfg, address])
    db.flush()
    order = models.Order(user_id=account.id, order_type="print", total_price=20, price=20, address_id=address.id, paypal_order_id="PAY-REVIEW")
    db.add(order)
    db.flush()
    db.add(models.OrderItem(order_id=order.id, model_type=cfg.model_type, price=20))
    db.commit()
    return order, cfg, address


def payment(order, status="APPROVED"):
    return {"status": status, "purchase_units": [{"custom_id": order.id, "amount": {"value": "20", "currency_code": "USD"}}]}


def test_no_stock_never_captures_payment(client, db, account, monkeypatch):
    order, cfg, _ = print_order(db, account, stock=0)
    monkeypatch.setattr(orders, "get_paypal_order_api", lambda _: payment(order))
    capture = MagicMock()
    monkeypatch.setattr(orders, "capture_paypal_order_api", capture)
    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": order.paypal_order_id})
    assert response.status_code == 409
    capture.assert_not_called()
    db.refresh(cfg)
    assert cfg.stock == 0


def test_multi_model_hold_rolls_back_all_stock_when_one_model_sells_out(db, account):
    order, cfg, _ = print_order(db, account)
    missing = models.ModelSalesLimit(model_type="z-last-model", order_type="print", stock=0, price=20)
    db.add_all([missing, models.OrderItem(order_id=order.id, model_type=missing.model_type, price=20)])
    db.commit()
    with pytest.raises(HTTPException) as exc:
        reserve_inventory(db, lock_order(db, order.id))
    assert exc.value.status_code == 409
    db.commit()
    db.refresh(cfg)
    db.refresh(order)
    assert cfg.stock == 1 and not order.inventory_reserved


def test_legacy_captured_order_without_stock_enters_fulfillment_review(client, db, account, monkeypatch):
    order, cfg, _ = print_order(db, account, stock=0)
    monkeypatch.setattr(orders, "get_paypal_order_api", lambda _: payment(order, "COMPLETED"))
    capture = MagicMock()
    monkeypatch.setattr(orders, "capture_paypal_order_api", capture)
    result = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": order.paypal_order_id})
    assert result.status_code == 200 and result.json()["goods_status"] == "awaiting_stock"
    capture.assert_not_called()
    db.refresh(cfg)
    assert cfg.stock == 0


def test_presigning_order_response_does_not_replace_stored_object_key(client, db, account, monkeypatch):
    order, _, _ = print_order(db, account)
    item = db.query(models.OrderItem).filter_by(order_id=order.id).one()
    item.skin_url = "orders/owned/item.png"
    db.commit()
    monkeypatch.setattr("s3_utils.generate_presigned_url_get", lambda *args, **kwargs: "https://signed.example.test/temporary")
    assert client.get(f"/skin/api/orders/{order.id}").json()["items"][0]["skin_url"].endswith("temporary")
    db.commit()
    db.refresh(item)
    assert item.skin_url == "orders/owned/item.png"


def test_timeout_then_background_repair_consumes_stock_once(client, db, account, monkeypatch):
    order, cfg, address = print_order(db, account)
    monkeypatch.setattr(orders, "get_paypal_order_api", lambda _: payment(order))
    monkeypatch.setattr(orders, "capture_paypal_order_api", MagicMock(side_effect=TimeoutError("response lost")))
    assert client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": order.paypal_order_id}).status_code == 503
    db.refresh(order)
    db.refresh(cfg)
    assert order.capture_started and order.inventory_reserved and cfg.stock == 0
    assert client.put(f"/skin/api/orders/{order.id}/cancel").status_code == 409
    db.rollback()
    monkeypatch.setattr(orders, "get_paypal_order_api", lambda _: payment(order, "COMPLETED"))
    asyncio.run(orders.repair_unhandled_orders(db))
    assert client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": order.paypal_order_id}).status_code == 200
    db.refresh(cfg)
    db.refresh(order)
    assert cfg.stock == 0 and order.status == "paid" and order.goods_status == "preparing"
    address.detail_address = "Changed address"
    db.commit()
    assert client.get(f"/skin/api/orders/{order.id}").json()["address"]["detail_address"] == "Original address"
    db.delete(address)
    db.commit()
    assert client.get(f"/skin/api/orders/{order.id}").json()["address"]["detail_address"] == "Original address"


def test_uncaptured_holds_expire_but_uncertain_captures_do_not(db, account):
    order, cfg, _ = print_order(db, account)
    reserve_inventory(db, lock_order(db, order.id))
    order.inventory_reserved_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    expire_inventory_holds(db)
    db.refresh(cfg)
    assert cfg.stock == 1
    assert order.paypal_order_id is None
    reserve_inventory(db, lock_order(db, order.id))
    order.inventory_reserved_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    order.capture_started = True
    db.commit()
    expire_inventory_holds(db)
    db.refresh(cfg)
    assert cfg.stock == 0 and order.inventory_reserved


def test_payment_network_wait_does_not_block_other_requests(account, monkeypatch):
    import httpx
    import threading
    started = threading.Event()
    release = threading.Event()
    def slow_paypal(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return {"id": "PAY-ASYNC", "links": []}
    monkeypatch.setattr("routers.credit.create_paypal_order_api", slow_paypal)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            slow = asyncio.create_task(client.post("/skin/api/credits/purchase", json={"amount": 1}))
            try:
                assert await asyncio.to_thread(started.wait, 1)
                fast = await asyncio.wait_for(client.get("/skin/api/orders/paypal/config"), timeout=0.5)
                assert fast.status_code == 200
            finally:
                release.set()
                assert (await slow).status_code == 200
    asyncio.run(run())
