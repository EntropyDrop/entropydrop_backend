from datetime import datetime, timedelta, timezone

import pytest

from auth import get_current_admin, get_current_user
from main import app
from models import CreditLog, User

LOOKUP = "/api/monitor/users/lookup"
GIFT = "/api/monitor/users/gift-pro-plus"


@pytest.fixture
def target(db):
    user = User(email="target@example.com", username="Target Player", credits=12)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def admin():
    app.dependency_overrides[get_current_admin] = lambda: User(email="admin@example.com")


def test_monitor_account_tools_require_admin(client, target):
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    payload = {"email": target.email, "expires_at": expiry.isoformat()}

    assert client.post(LOOKUP, json=payload).status_code == 401
    assert client.post(GIFT, json=payload).status_code == 401

    app.dependency_overrides[get_current_user] = lambda: target
    assert client.post(LOOKUP, json=payload).status_code == 403
    assert client.post(GIFT, json=payload).status_code == 403


def test_lookup_account_by_email(client, db, admin, target):
    response = client.post(LOOKUP, json={"email": " TARGET@example.com "})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"email": target.email, "username": "Target Player"}

    target.username = None
    db.commit()
    assert client.post(LOOKUP, json={"email": target.email}).json()["username"] is None
    assert client.post(LOOKUP, json={"email": "missing@example.com"}).status_code == 404

    db.add(User(email="TARGET@example.com"))
    db.commit()
    response = client.post(LOOKUP, json={"email": target.email})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ambiguous_email"


def test_gift_pro_plus_by_email(client, db, admin, target):
    target.pro_level = "pro-max"
    target.pro_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    local_zone = timezone(timedelta(hours=8))
    expiry = datetime.now(local_zone) + timedelta(days=30)
    payload = {"email": "TARGET@example.com", "expires_at": expiry.isoformat()}
    for _ in range(2):
        response = client.post(GIFT, json=payload)
        assert response.status_code == 200
        assert response.json()["email"] == target.email
        assert datetime.fromisoformat(response.json()["expires_at"].replace("Z", "+00:00")) == expiry

    db.refresh(target)
    assert target.is_pro
    assert target.pro_level == "pro-plus"
    assert target.pro_expires_at.replace(tzinfo=timezone.utc) == expiry
    assert target.credits == 12
    assert target.paypal_subscription_id is None
    assert db.query(CreditLog).filter_by(user_id=target.id).count() == 0


def test_gift_pro_plus_rejects_invalid_or_conflicting_expiry(client, db, admin, target):
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=30)

    def gift(expires_at, email=target.email):
        return client.post(GIFT, json={"email": email, "expires_at": expires_at.isoformat()})

    assert gift(now - timedelta(minutes=1)).status_code == 422
    assert gift(expiry.replace(tzinfo=None)).status_code == 422
    assert gift(expiry, email="missing@example.com").status_code == 404

    target.pro_level = "pro-max"
    target.pro_expires_at = expiry
    db.commit()
    response = gift(expiry + timedelta(days=1))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_pro_max"
    assert target.pro_level == "pro-max"

    target.pro_level = "pro-plus"
    db.commit()
    response = gift(expiry - timedelta(days=1))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "expiry_before_current"
    assert target.pro_expires_at.replace(tzinfo=timezone.utc) == expiry
    assert gift(expiry + timedelta(days=1)).status_code == 200


def test_paid_pro_plus_activation_preserves_later_gift_expiry(client, db, admin, target, monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: target
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=90)
    response = client.post(GIFT, json={"email": target.email, "expires_at": expiry.isoformat()})
    assert response.status_code == 200

    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.get_paypal_subscription_api", lambda _sub_id: {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": target.email},
        "billing_info": {
            "last_payment": {"time": now.isoformat(), "amount": {"value": "8.00", "currency_code": "USD"}},
            "next_billing_time": (now + timedelta(days=30)).isoformat(),
        },
    })
    response = client.post("/api/orders/subscription/activate", json={"paypal_order_id": "SUB-GIFT"})
    assert response.status_code == 200
    db.refresh(target)
    assert target.paypal_subscription_id == "SUB-GIFT"
    assert target.pro_expires_at.replace(tzinfo=timezone.utc) == expiry
