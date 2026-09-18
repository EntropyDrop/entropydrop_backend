import json

import models
import payment_utils


def test_paypal_webhook_rejects_missing_webhook_id(monkeypatch, client):
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "")

    response = client.post("/skin/api/webhooks/paypal", json={"event_type": "BILLING.SUBSCRIPTION.ACTIVATED"})

    assert response.status_code == 400
    assert response.json()["detail"] == "PayPal webhook ID is not configured"


def test_paypal_webhook_accepts_lowercase_headers_after_signature(monkeypatch, client, db):
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")

    def fake_verify(headers, body, webhook_id):
        assert webhook_id == "WH-123"
        assert headers["paypal-transmission-id"] == "T-1"
        return True

    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", fake_verify)
    user = models.User(
        id="USERWEBHOOK0001",
        email="webhook@example.com",
        paypal_subscription_id="SUB-123",
    )
    db.add(user)
    db.commit()

    response = client.post(
        "/skin/api/webhooks/paypal",
        json={
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "SUB-123"},
        },
        headers={"paypal-transmission-id": "T-1"},
    )

    assert response.status_code == 200
    db.refresh(user)
    assert user.paypal_subscription_status == "ACTIVE"


def test_verify_paypal_webhook_signature_reads_lowercase_headers(monkeypatch):
    monkeypatch.setattr(payment_utils, "get_paypal_access_token", lambda: "ACCESS")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"verification_status": "SUCCESS"}

    def fake_post(url, json, headers, timeout):
        assert json["auth_algo"] == "SHA256withRSA"
        assert json["cert_url"] == "https://example.com/cert.pem"
        assert json["transmission_id"] == "T-1"
        assert json["transmission_sig"] == "sig"
        assert json["transmission_time"] == "2026-05-27T00:00:00Z"
        assert json["webhook_id"] == "WH-123"
        assert headers["Authorization"] == "Bearer ACCESS"
        return FakeResponse()

    monkeypatch.setattr(payment_utils.requests, "post", fake_post)

    is_valid = payment_utils.verify_paypal_webhook_signature(
        {
            "paypal-auth-algo": "SHA256withRSA",
            "paypal-cert-url": "https://example.com/cert.pem",
            "paypal-transmission-id": "T-1",
            "paypal-transmission-sig": "sig",
            "paypal-transmission-time": "2026-05-27T00:00:00Z",
        },
        json.dumps({"id": "EVT-1"}).encode("utf-8"),
        "WH-123",
    )

    assert is_valid is True


def test_paypal_webhook_dual_route_compatibility(monkeypatch, client, db):
    """Verify PayPal webhooks work identically on both new /api/webhooks/paypal and legacy /skin/api/webhooks/paypal."""
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda headers, body, webhook_id: True)

    user_new = models.User(
        id="USERWHNEW0001",
        email="webhook_new@example.com",
        paypal_subscription_id="SUB-NEW-123",
    )
    user_legacy = models.User(
        id="USERWHLEGACY01",
        email="webhook_legacy@example.com",
        paypal_subscription_id="SUB-LEGACY-123",
    )
    db.add_all([user_new, user_legacy])
    db.commit()

    # 1. Test standard route /api/webhooks/paypal
    resp_new = client.post(
        "/api/webhooks/paypal",
        json={
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "SUB-NEW-123"},
        },
        headers={"paypal-transmission-id": "T-NEW"},
    )
    assert resp_new.status_code == 200
    db.refresh(user_new)
    assert user_new.paypal_subscription_status == "ACTIVE"

    # 2. Test legacy route /skin/api/webhooks/paypal
    resp_legacy = client.post(
        "/skin/api/webhooks/paypal",
        json={
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "SUB-LEGACY-123"},
        },
        headers={"paypal-transmission-id": "T-LEGACY"},
    )
    assert resp_legacy.status_code == 200
    db.refresh(user_legacy)
    assert user_legacy.paypal_subscription_status == "ACTIVE"

    # 3. Test cancellation via standard route
    resp_cancel = client.post(
        "/api/webhooks/paypal",
        json={
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "SUB-NEW-123"},
        },
        headers={"paypal-transmission-id": "T-CANCEL"},
    )
    assert resp_cancel.status_code == 200
    db.refresh(user_new)
    assert user_new.paypal_subscription_status == "CANCELLED"

    # 4. Test cancellation via legacy route
    resp_cancel_legacy = client.post(
        "/skin/api/webhooks/paypal",
        json={
            "event_type": "BILLING.SUBSCRIPTION.CANCELLED",
            "resource": {"id": "SUB-LEGACY-123"},
        },
        headers={"paypal-transmission-id": "T-CANCEL-LEGACY"},
    )
    assert resp_cancel_legacy.status_code == 200
    db.refresh(user_legacy)
    assert user_legacy.paypal_subscription_status == "CANCELLED"


def test_paypal_webhook_subscription_payment_sale_completed_dual_route(monkeypatch, client, db):
    """Verify recurring payment (PAYMENT.SALE.COMPLETED) works across both legacy /skin and new /api routes."""
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PRO-PLUS")
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_PRO_MAX_PLAN_ID", "PLAN-PRO-MAX")
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda headers, body, webhook_id: True)

    def fake_get_subscription(sub_id):
        return {
            "id": sub_id,
            "plan_id": "PLAN-PRO-PLUS",
            "billing_info": {
                "last_payment": {
                    "time": "2026-09-14T00:00:00Z",
                    "amount": {"value": "20.00", "currency_code": "USD"},
                },
                "next_billing_time": "2026-10-14T00:00:00Z",
            },
        }

    monkeypatch.setattr("payment_utils.get_paypal_subscription_api", fake_get_subscription)

    # User 1: receiving renewal webhook on legacy route /skin/api/webhooks/paypal (the currently configured PayPal webhook URL)
    user_legacy = models.User(
        id="USERRENEWLEGACY",
        email="renew_legacy@example.com",
        paypal_subscription_id="SUB-RENEW-LEGACY",
        credits=0,
    )
    # User 2: receiving renewal webhook on new standard route /api/webhooks/paypal
    user_new = models.User(
        id="USERRENEWNEW",
        email="renew_new@example.com",
        paypal_subscription_id="SUB-RENEW-NEW",
        credits=0,
    )
    db.add_all([user_legacy, user_new])
    db.commit()

    # 1. Renewal event via legacy /skin/api/webhooks/paypal
    resp_legacy = client.post(
        "/skin/api/webhooks/paypal",
        json={
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-LEGACY-001",
                "billing_agreement_id": "SUB-RENEW-LEGACY",
                "amount": {"total": "20.00", "currency": "USD"},
            },
        },
        headers={"paypal-transmission-id": "T-SALE-LEGACY"},
    )
    assert resp_legacy.status_code == 200
    assert resp_legacy.json() == {"status": "success"}
    db.refresh(user_legacy)
    assert user_legacy.paypal_subscription_status == "ACTIVE"
    assert user_legacy.pro_level == "pro-plus"
    assert user_legacy.credits == 80
    assert user_legacy.pro_expires_at is not None

    # Order created
    legacy_order = db.query(models.Order).filter(models.Order.paypal_order_id == "SALE-LEGACY-001").first()
    assert legacy_order is not None
    assert legacy_order.user_id == user_legacy.id
    assert legacy_order.status == "paid"

    # 2. Renewal event via new /api/webhooks/paypal
    resp_new = client.post(
        "/api/webhooks/paypal",
        json={
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-NEW-001",
                "billing_agreement_id": "SUB-RENEW-NEW",
                "amount": {"total": "20.00", "currency": "USD"},
            },
        },
        headers={"paypal-transmission-id": "T-SALE-NEW"},
    )
    assert resp_new.status_code == 200
    assert resp_new.json() == {"status": "success"}
    db.refresh(user_new)
    assert user_new.paypal_subscription_status == "ACTIVE"
    assert user_new.pro_level == "pro-plus"
    assert user_new.credits == 80
    assert user_new.pro_expires_at is not None

    # Order created
    new_order = db.query(models.Order).filter(models.Order.paypal_order_id == "SALE-NEW-001").first()
    assert new_order is not None
    assert new_order.user_id == user_new.id
    assert new_order.status == "paid"


def test_payment_webhook_links_verified_custom_id_before_browser_activation(monkeypatch, client, db):
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PRO-PLUS")
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda *args: True)

    user = models.User(
        id="USERRACE00000001",
        email="race@example.com",
        credits=0,
    )
    db.add(user)
    db.commit()

    monkeypatch.setattr(
        "payment_utils.get_paypal_subscription_api",
        lambda sub_id: {
            "id": sub_id,
            "status": "ACTIVE",
            "plan_id": "PLAN-PRO-PLUS",
            "custom_id": user.id,
            "billing_info": {
                "last_payment": {
                    "time": "2026-09-17T15:13:35Z",
                    "amount": {"value": "8.0", "currency_code": "USD"},
                },
                "next_billing_time": "2026-10-17T10:00:00Z",
            },
        },
    )
    event = {
        "id": "WH-RACE-1",
        "event_type": "PAYMENT.SALE.COMPLETED",
        "resource": {
            "id": "SALE-RACE-1",
            "billing_agreement_id": "SUB-RACE-1",
            "amount": {"total": "8.00", "currency": "USD"},
        },
    }

    for _ in range(2):
        response = client.post("/api/webhooks/paypal", json=event)
        assert response.status_code == 200
        assert response.json() == {"status": "success"}

    db.refresh(user)
    assert user.paypal_subscription_id == "SUB-RACE-1"
    assert user.paypal_subscription_status == "ACTIVE"
    assert user.pro_level == "pro-plus"
    assert user.credits == 80
    assert db.query(models.Order).filter_by(paypal_order_id="SALE-RACE-1").count() == 1
    assert db.query(models.CreditLog).filter_by(action="subscription_grant").count() == 1


def test_activation_webhook_links_verified_custom_id(monkeypatch, client, db):
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PRO-PLUS")
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda *args: True)

    user = models.User(id="USERACTIVE000001", email="active@example.com")
    db.add(user)
    db.commit()
    monkeypatch.setattr(
        "payment_utils.get_paypal_subscription_api",
        lambda sub_id: {
            "id": sub_id,
            "status": "ACTIVE",
            "plan_id": "PLAN-PRO-PLUS",
            "custom_id": user.id,
        },
    )

    response = client.post(
        "/api/webhooks/paypal",
        json={
            "id": "WH-ACTIVE-1",
            "event_type": "BILLING.SUBSCRIPTION.ACTIVATED",
            "resource": {"id": "SUB-ACTIVE-1"},
        },
    )

    assert response.status_code == 200
    db.refresh(user)
    assert user.paypal_subscription_id == "SUB-ACTIVE-1"
    assert user.paypal_subscription_status == "ACTIVE"
    assert user.pro_level == "free"
    assert user.credits == 0


def test_payment_webhook_retries_when_verified_owner_is_unknown(monkeypatch, client, db):
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_WEBHOOK_ID", "WH-123")
    monkeypatch.setattr("routers.webhooks.settings.PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PRO-PLUS")
    monkeypatch.setattr("payment_utils.verify_paypal_webhook_signature", lambda *args: True)
    monkeypatch.setattr(
        "payment_utils.get_paypal_subscription_api",
        lambda sub_id: {
            "id": sub_id,
            "status": "ACTIVE",
            "plan_id": "PLAN-PRO-PLUS",
            "custom_id": "MISSINGUSER00001",
            "billing_info": {
                "last_payment": {
                    "time": "2026-09-17T15:13:35Z",
                    "amount": {"value": "8.00", "currency_code": "USD"},
                },
                "next_billing_time": "2026-10-17T10:00:00Z",
            },
        },
    )

    response = client.post(
        "/api/webhooks/paypal",
        json={
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-NO-OWNER",
                "billing_agreement_id": "SUB-NO-OWNER",
                "amount": {"total": "8.00", "currency": "USD"},
            },
        },
    )

    assert response.status_code == 409
    assert db.query(models.Order).filter_by(paypal_order_id="SALE-NO-OWNER").count() == 0
