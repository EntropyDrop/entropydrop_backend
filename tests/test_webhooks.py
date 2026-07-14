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
