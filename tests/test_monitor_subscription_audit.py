import pytest
from datetime import datetime, timezone, timedelta
from main import app
from auth import get_current_admin
import models


@pytest.fixture
def admin_user(db):
    admin = models.User(
        id="ADMINUSER0000001",
        email="admin@entropydrop.com",
        username="admin",
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def test_subscription_credit_audit_requires_admin(client):
    resp = client.get("/api/monitor/subscription-credit-audits")
    assert resp.status_code in (401, 403)


def test_subscription_credit_audit_success_and_anomalies(client, db, admin_user):
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    try:
        now = datetime.now(timezone.utc)

        # 1. Normal Pro Plus user: paid order + 80 credit grant
        user_plus = models.User(
            id="USERPLUS00000001",
            email="plus_ok@example.com",
            username="plus_user",
            pro_level="pro-plus",
            paypal_subscription_id="SUB-PLUS-OK",
            credits=80,
        )
        # 2. Normal Pro Max user: paid order + 200 credit grant
        user_max = models.User(
            id="USERMAX000000001",
            email="max_ok@example.com",
            username="max_user",
            pro_level="pro-max",
            paypal_subscription_id="SUB-MAX-OK",
            credits=200,
        )
        # 3. Anomaly user: paid order BUT NO credit grant
        user_missing = models.User(
            id="USERMISS00000001",
            email="missing@example.com",
            username="miss_user",
            pro_level="pro-plus",
            paypal_subscription_id="SUB-MISSING",
            credits=0,
        )
        # 4. Mismatch user: paid for Pro Max (200 credits expected), but only received 80 credits
        user_mismatch = models.User(
            id="USERMISMATCH0001",
            email="mismatch@example.com",
            username="mismatch_user",
            pro_level="pro-max",
            paypal_subscription_id="SUB-MISMATCH",
            credits=80,
        )
        db.add_all([user_plus, user_max, user_missing, user_mismatch])
        db.commit()

        # Orders
        order_plus = models.Order(
            id="ORDPLUS000000001",
            user_id=user_plus.id,
            order_type="subscription",
            status="paid",
            price=20.0,
            paypal_order_id="SALE-PLUS-01",
            paid_at=now - timedelta(days=2),
        )
        order_max = models.Order(
            id="ORDMAX0000000001",
            user_id=user_max.id,
            order_type="subscription",
            status="paid",
            price=50.0,
            paypal_order_id="SALE-MAX-01",
            paid_at=now - timedelta(days=1),
        )
        order_missing = models.Order(
            id="ORDMISS000000001",
            user_id=user_missing.id,
            order_type="subscription",
            status="paid",
            price=20.0,
            paypal_order_id="SALE-MISS-01",
            paid_at=now - timedelta(hours=5),
        )
        order_mismatch = models.Order(
            id="ORDMISMATCH00001",
            user_id=user_mismatch.id,
            order_type="subscription",
            status="paid",
            price=50.0,
            paypal_order_id="SALE-MISMATCH-01",
            paid_at=now - timedelta(hours=2),
        )
        db.add_all([order_plus, order_max, order_missing, order_mismatch])
        db.flush()

        # Order items
        item_plus = models.OrderItem(order_id=order_plus.id, model_type="pro-plus", price=20.0)
        item_max = models.OrderItem(order_id=order_max.id, model_type="pro-max", price=50.0)
        item_missing = models.OrderItem(order_id=order_missing.id, model_type="pro-plus", price=20.0)
        item_mismatch = models.OrderItem(order_id=order_mismatch.id, model_type="pro-max", price=50.0)
        db.add_all([item_plus, item_max, item_missing, item_mismatch])

        # Credit logs
        log_plus = models.CreditLog(
            user_id=user_plus.id,
            amount=80,
            action="subscription_grant",
            source="Subscription Webhook Grant: SUB-PLUS-OK",
            idempotency_key=f"subscription:SUB-PLUS-OK:{(now - timedelta(days=2)).isoformat()}",
            created_at=now - timedelta(days=2),
        )
        log_max = models.CreditLog(
            user_id=user_max.id,
            amount=200,
            action="subscription_grant",
            source="Subscription Webhook Grant: SUB-MAX-OK",
            idempotency_key=f"subscription:SUB-MAX-OK:{(now - timedelta(days=1)).isoformat()}",
            created_at=now - timedelta(days=1),
        )
        log_mismatch = models.CreditLog(
            user_id=user_mismatch.id,
            amount=80,  # Should have been 200
            action="subscription_grant",
            source="Subscription Webhook Grant: SUB-MISMATCH",
            idempotency_key=f"subscription:SUB-MISMATCH:{(now - timedelta(hours=2)).isoformat()}",
            created_at=now - timedelta(hours=2),
        )
        db.add_all([log_plus, log_max, log_mismatch])
        db.commit()

        # 1. Query all audits on standard route
        resp = client.get("/api/monitor/subscription-credit-audits")
        assert resp.status_code == 200
        data = resp.json()

        # Check summary
        summary = data["summary"]
        assert summary["total_orders"] == 4
        assert summary["success_count"] == 2
        assert summary["anomaly_count"] == 2
        assert summary["total_credits_granted"] == 360  # 80 + 200 + 80
        assert summary["total_revenue"] == 140.0

        # Check items
        items = data["items"]
        assert len(items) == 4

        item_by_id = {item["order_id"]: item for item in items}
        assert item_by_id["ORDPLUS000000001"]["grant_status"] == "success"
        assert item_by_id["ORDPLUS000000001"]["expected_credits"] == 80
        assert item_by_id["ORDPLUS000000001"]["granted_credits"] == 80

        assert item_by_id["ORDMAX0000000001"]["grant_status"] == "success"
        assert item_by_id["ORDMAX0000000001"]["expected_credits"] == 200
        assert item_by_id["ORDMAX0000000001"]["granted_credits"] == 200

        assert item_by_id["ORDMISS000000001"]["grant_status"] == "missing"
        assert item_by_id["ORDMISS000000001"]["expected_credits"] == 80
        assert item_by_id["ORDMISS000000001"]["granted_credits"] == 0

        assert item_by_id["ORDMISMATCH00001"]["grant_status"] == "mismatch"
        assert item_by_id["ORDMISMATCH00001"]["expected_credits"] == 200
        assert item_by_id["ORDMISMATCH00001"]["granted_credits"] == 80

        # 2. Dual route verification: /skin/api/monitor/subscription-credit-audits
        legacy_resp = client.get("/skin/api/monitor/subscription-credit-audits")
        assert legacy_resp.status_code == 200
        assert legacy_resp.json()["summary"] == summary

        # 3. Filter by anomaly
        anomaly_resp = client.get("/api/monitor/subscription-credit-audits?status_filter=anomaly")
        assert anomaly_resp.status_code == 200
        anomaly_data = anomaly_resp.json()
        assert anomaly_data["total_count"] == 2
        anomaly_order_ids = [it["order_id"] for it in anomaly_data["items"]]
        assert "ORDMISS000000001" in anomaly_order_ids
        assert "ORDMISMATCH00001" in anomaly_order_ids

        # 4. Filter by success
        success_resp = client.get("/api/monitor/subscription-credit-audits?status_filter=success")
        assert success_resp.status_code == 200
        success_data = success_resp.json()
        assert success_data["total_count"] == 2
        success_order_ids = [it["order_id"] for it in success_data["items"]]
        assert "ORDPLUS000000001" in success_order_ids
        assert "ORDMAX0000000001" in success_order_ids

        # 5. Search by keyword
        search_resp = client.get("/api/monitor/subscription-credit-audits?search=missing@example.com")
        assert search_resp.status_code == 200
        search_data = search_resp.json()
        assert search_data["total_count"] == 1
        assert search_data["items"][0]["order_id"] == "ORDMISS000000001"

    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]


def test_compensate_subscription_credits(client, db, admin_user):
    app.dependency_overrides[get_current_admin] = lambda: admin_user
    try:
        now = datetime.now(timezone.utc)
        user = models.User(
            id="COMPUSER00000001",
            email="compensate_me@example.com",
            username="comp_user",
            pro_level="pro-plus",
            paypal_subscription_id="SUB-COMP-01",
            credits=10,
        )
        db.add(user)
        db.commit()

        order = models.Order(
            id="ORDCOMP000000001",
            user_id=user.id,
            order_type="subscription",
            status="paid",
            price=20.0,
            paypal_order_id="SALE-COMP-01",
            paid_at=now,
        )
        db.add(order)
        db.flush()

        item = models.OrderItem(order_id=order.id, model_type="pro-plus", price=20.0)
        db.add(item)
        db.commit()

        # Initially, audit should report missing
        audit_before = client.get(f"/api/monitor/subscription-credit-audits?search={order.id}").json()
        assert audit_before["items"][0]["grant_status"] == "missing"

        # Compensate
        comp_resp = client.post(f"/api/monitor/subscription-credit-audits/{order.id}/compensate")
        assert comp_resp.status_code == 200
        comp_data = comp_resp.json()
        assert comp_data["status"] == "success"
        assert comp_data["compensated_credits"] == 80
        assert comp_data["new_user_credits"] == 90  # 10 + 80

        db.refresh(user)
        assert user.credits == 90

        # Verified credit log was recorded
        comp_log = db.query(models.CreditLog).filter(models.CreditLog.id == comp_data["credit_log_id"]).first()
        assert comp_log is not None
        assert comp_log.amount == 80
        assert comp_log.action == "subscription_grant"
        assert f"Order {order.id}" in comp_log.source

        # Cannot compensate again (already granted)
        double_comp_resp = client.post(f"/api/monitor/subscription-credit-audits/{order.id}/compensate")
        assert double_comp_resp.status_code == 400

        # After compensation, audit reports success
        audit_after = client.get(f"/api/monitor/subscription-credit-audits?search={order.id}").json()
        assert audit_after["items"][0]["grant_status"] == "success"
        assert audit_after["items"][0]["granted_credits"] == 80

    finally:
        if get_current_admin in app.dependency_overrides:
            del app.dependency_overrides[get_current_admin]
