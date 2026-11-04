from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

import auth
import models
import payment_utils
import subscription_lifecycle as lifecycle
from main import app
from routers import order


@pytest.fixture
def account(db, monkeypatch):
    user = models.User(id="subscription-test", email="subscriber@example.test", credits=0)
    db.add(user)
    db.commit()
    app.dependency_overrides[auth.get_current_user] = lambda: user
    monkeypatch.setattr(order.settings, "PAYPAL_PRO_PLUS_PLAN_ID", "PLAN-PLUS")
    monkeypatch.setattr(order.settings, "PAYPAL_PRO_MAX_PLAN_ID", "PLAN-MAX")
    monkeypatch.setattr(order.settings, "PAYPAL_WEBHOOK_ID", "WEBHOOK")
    monkeypatch.setattr(order.backend_utils, "is_payment_enabled", lambda: True)
    monkeypatch.setattr(payment_utils, "verify_paypal_webhook_signature", lambda *args: True)
    yield user
    app.dependency_overrides.pop(auth.get_current_user, None)


@pytest.fixture
def paypal(account, monkeypatch):
    remote, requests_by_key, cancels, creates = {}, {}, [], []

    def data(sid, status="ACTIVE", days=2):
        paid = lifecycle.now() - timedelta(days=days)
        return {"id": sid, "custom_id": account.id, "status": status, "plan_id": "PLAN-PLUS",
                "create_time": paid.isoformat(), "status_update_time": paid.isoformat(),
                "billing_info": {"last_payment": {"time": paid.isoformat(),
                    "amount": {"value": "8", "currency_code": "USD"}}},
                "links": [{"rel": "approve", "href": "https://www.paypal.com/approve/" + sid}]}

    def create(**kwargs):
        creates.append(kwargs)
        key = kwargs["request_id"]
        if key not in requests_by_key:
            sid = "SUB-" + str(len(requests_by_key) + 1)
            requests_by_key[key] = sid
            remote[sid] = data(sid, "APPROVAL_PENDING", days=0)
        return deepcopy(remote[requests_by_key[key]])

    def get(sid):
        return deepcopy(remote[sid])

    def cancel(sid, **kwargs):
        cancels.append(sid)
        remote[sid]["status"] = "CANCELLED"
        remote[sid]["status_update_time"] = lifecycle.now().isoformat()
        return True

    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", create)
    monkeypatch.setattr(payment_utils, "get_paypal_subscription_api", get)
    monkeypatch.setattr(order, "get_paypal_subscription_api", get)
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", cancel)
    return {"remote":remote,"data":data,"create":create,"get":get,"cancel":cancel,
            "cancels":cancels,"creates":creates}


def create(client, tier="pro_plus", url="https://entropydrop.com/credits"):
    return client.post("/api/orders/subscription/create", json={"tier":tier,"return_url":url})


def event(client, sid, kind="CANCELLED"):
    return client.post("/api/webhooks/paypal", json={"event_type":"BILLING.SUBSCRIPTION." + kind,
        "resource":{"id":sid}})


def bind(db, user, paypal, sid="SUB-CURRENT", status="ACTIVE"):
    paypal["remote"][sid] = paypal["data"](sid, status)
    user.paypal_subscription_id = sid
    user.paypal_subscription_status = status
    db.commit()


def test_repeated_checkout_reuses_one_contract_then_cancel_stops_it(client, db, account, paypal):
    first, second = create(client), create(client)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(paypal["creates"]) == 1
    sid = first.json()["subscription_id"]
    paypal["remote"][sid]["status"] = "ACTIVE"
    assert client.post("/api/orders/subscription/activate", json={"paypal_order_id":sid}).status_code == 200
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert all(row["status"] == "CANCELLED" for row in paypal["remote"].values())
    assert db.query(models.PaypalSubscription).count() == 1


def test_old_cancellation_cannot_hide_or_prevent_cancelling_current_subscription(client, db, account, paypal):
    bind(db, account, paypal)
    paypal["remote"]["SUB-OLD"] = paypal["data"]("SUB-OLD", "CANCELLED", days=50)
    assert event(client, "SUB-OLD").status_code == 200
    db.refresh(account)
    assert (account.paypal_subscription_id, account.paypal_subscription_status) == ("SUB-CURRENT", "ACTIVE")
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert paypal["remote"]["SUB-CURRENT"]["status"] == "CANCELLED"


@pytest.mark.parametrize("kind", ["ACTIVATED", "SALE"])
def test_late_event_preserves_cancelled_state_and_records_paid_entitlements(client, db, account, paypal, kind):
    bind(db, account, paypal, status="CANCELLED")
    if kind == "SALE":
        response = client.post("/api/webhooks/paypal", json={"event_type":"PAYMENT.SALE.COMPLETED",
            "resource":{"id":"SALE-1","billing_agreement_id":"SUB-CURRENT","amount":{"total":"8","currency":"USD"}}})
        assert db.query(models.Order).count() == 1
        assert account.credits == 80
    else:
        response = event(client, "SUB-CURRENT", kind)
    assert response.status_code == 200
    db.refresh(account)
    assert account.paypal_subscription_status == "CANCELLED"


def test_provider_confirmed_terminal_state_survives_stale_active_snapshot(client, db, account, paypal):
    bind(db, account, paypal)
    assert event(client, "SUB-CURRENT", "CANCELLED").status_code == 200
    # The GET response still says ACTIVE, but the signed terminal event wins.
    assert event(client, "SUB-CURRENT", "ACTIVATED").status_code == 200
    db.refresh(account)
    assert account.paypal_subscription_status == "CANCELLED"


def test_signed_cancellation_wins_over_outdated_provider_timestamp(client, db, account, paypal):
    bind(db, account, paypal)
    lifecycle.remember(db, account, "SUB-CURRENT", paypal["get"]("SUB-CURRENT"))
    db.query(models.PaypalSubscription).one().provider_updated_at = lifecycle.now()
    db.commit()
    assert event(client, "SUB-CURRENT").status_code == 200
    assert account.paypal_subscription_status == "CANCELLED"


@pytest.mark.parametrize("through_webhook", [True, False])
def test_legacy_second_approval_is_cancelled_without_losing_first(client, db, account, paypal, through_webhook):
    bind(db, account, paypal)
    paypal["remote"]["SUB-SECOND"] = paypal["data"]("SUB-SECOND")
    if through_webhook:
        assert event(client, "SUB-SECOND", "ACTIVATED").status_code == 200
    else:
        assert client.post("/api/orders/subscription/activate", json={"paypal_order_id":"SUB-SECOND"}).status_code == 409
    assert paypal["remote"]["SUB-SECOND"]["status"] == "CANCELLED"
    db.refresh(account)
    assert account.paypal_subscription_id == "SUB-CURRENT"
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert paypal["remote"]["SUB-CURRENT"]["status"] == "CANCELLED"


def test_cancel_all_known_contracts_even_when_display_says_cancelled(client, db, account, paypal):
    bind(db, account, paypal)
    account.paypal_subscription_status = "CANCELLED"  # Historical wrong local state.
    paypal["remote"]["SUB-OTHER"] = paypal["data"]("SUB-OTHER")
    db.add(models.PaypalSubscription(user_id=account.id, paypal_id="SUB-OTHER", status="ACTIVE"))
    db.commit()
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert set(paypal["cancels"]) == {"SUB-CURRENT", "SUB-OTHER"}
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert len(paypal["cancels"]) == 2


def test_cancel_timeout_is_persisted_blocks_new_checkout_and_worker_retries(client, db, account, paypal, monkeypatch):
    bind(db, account, paypal)
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", Mock(side_effect=requests.Timeout()))
    assert client.post("/api/users/me/cancel_subscription").status_code == 503
    db.expire_all()
    row = db.query(models.PaypalSubscription).one()
    assert row.cancel_requested_at and not row.cancelled_at
    assert account.paypal_subscription_status == "CANCELLATION_PENDING"
    assert create(client).status_code == 409
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", paypal["cancel"])
    from conftest import TestingSessionLocal
    monkeypatch.setattr("database.SessionLocal", TestingSessionLocal)
    row.next_check_at = lifecycle.now() - timedelta(seconds=1)
    db.commit()
    lifecycle.reconcile_cancellations()
    db.expire_all()
    assert db.query(models.PaypalSubscription).one().cancelled_at
    assert account.paypal_subscription_status == "CANCELLED"


def test_cancel_timeout_after_provider_success_is_confirmed_by_get(client, db, account, paypal, monkeypatch):
    bind(db, account, paypal)
    def cancel(*args, **kwargs):
        paypal["cancel"](*args, **kwargs)
        raise requests.Timeout()
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", cancel)
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert account.paypal_subscription_status == "CANCELLED"


def test_partial_cancellation_does_not_report_success_and_retries_remaining_contract(client, db, account, paypal, monkeypatch):
    bind(db, account, paypal)
    paypal["remote"]["SUB-OTHER"] = paypal["data"]("SUB-OTHER")
    db.add(models.PaypalSubscription(user_id=account.id, paypal_id="SUB-OTHER", status="ACTIVE"))
    db.commit()
    def partial(sid, **kwargs):
        if sid == "SUB-OTHER":
            raise requests.Timeout()
        return paypal["cancel"](sid, **kwargs)
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", partial)
    assert client.post("/api/users/me/cancel_subscription").status_code == 503
    assert paypal["remote"]["SUB-CURRENT"]["status"] == "CANCELLED"
    assert paypal["remote"]["SUB-OTHER"]["status"] == "ACTIVE"
    assert account.paypal_subscription_status == "CANCELLATION_PENDING"
    monkeypatch.setattr(payment_utils, "cancel_paypal_subscription_api", paypal["cancel"])
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    assert paypal["remote"]["SUB-OTHER"]["status"] == "CANCELLED"


def test_creation_timeout_replays_same_request_and_original_payload(client, db, account, paypal, monkeypatch):
    def lost_response(**kwargs):
        paypal["create"](**kwargs)
        raise requests.Timeout()
    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", lost_response)
    assert create(client).status_code == 503
    assert db.query(models.PaypalSubscription).one().request_id
    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", paypal["create"])
    assert create(client, url="https://entropydrop.com/pro").status_code == 200
    assert len(paypal["remote"]) == 1
    assert paypal["creates"][0] == paypal["creates"][1]


def test_creation_timeout_with_early_webhook_merges_the_same_contract(client, db, account, paypal, monkeypatch):
    def lost_response(**kwargs):
        paypal["create"](**kwargs)
        raise requests.Timeout()
    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", lost_response)
    assert create(client).status_code == 503
    assert event(client, "SUB-1", "CREATED").status_code == 200
    monkeypatch.setattr(payment_utils, "create_paypal_subscription_api", paypal["create"])
    assert create(client).status_code == 200
    assert db.query(models.PaypalSubscription).count() == 1


def test_expired_creation_idempotency_window_never_creates_again(client, db, account, paypal):
    db.add(models.PaypalSubscription(user_id=account.id, request_id="stale-request", plan_id="PLAN-PLUS",
        return_url="https://entropydrop.com/credits", create_requested_at=lifecycle.now()-timedelta(days=4)))
    db.commit()
    assert create(client).status_code == 409
    assert not paypal["creates"]


def test_cancellation_before_unknown_old_approval_prevents_renewal(client, db, account, paypal):
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    paypal["remote"]["SUB-OLD"] = paypal["data"]("SUB-OLD", days=1)
    assert event(client, "SUB-OLD", "ACTIVATED").status_code == 200
    assert paypal["remote"]["SUB-OLD"]["status"] == "CANCELLED"


def test_resubscribe_after_confirmed_cancellation_uses_new_contract(client, db, account, paypal):
    bind(db, account, paypal)
    assert client.post("/api/users/me/cancel_subscription").status_code == 200
    response = create(client)
    assert response.status_code == 200
    assert response.json()["subscription_id"] != "SUB-CURRENT"
    assert db.query(models.PaypalSubscription).count() == 2


def test_pending_different_tier_does_not_open_second_contract(client, db, account, paypal):
    assert create(client).status_code == 200
    assert create(client, tier="pro_max").status_code == 409
    assert len(paypal["remote"]) == 1


def test_create_transport_sends_stable_idempotency_header(monkeypatch):
    monkeypatch.setattr(payment_utils, "get_paypal_access_token", lambda: "test")
    response = Mock()
    response.json.return_value = {"id":"SUB-1"}
    post = Mock(return_value=response)
    monkeypatch.setattr(payment_utils.requests, "post", post)
    payment_utils.create_paypal_subscription_api("PLAN-PLUS", "user", "https://example.test", "https://example.test", request_id="stable-key")
    assert post.call_args.kwargs["headers"]["PayPal-Request-Id"] == "stable-key"


def test_cancel_transport_requires_explicit_204_confirmation(monkeypatch):
    monkeypatch.setattr(payment_utils, "get_paypal_access_token", lambda: "test")
    monkeypatch.setattr(payment_utils.requests, "post", Mock(return_value=Mock(status_code=200)))
    with pytest.raises(RuntimeError):
        payment_utils.cancel_paypal_subscription_api("SUB-1")
