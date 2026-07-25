import pytest
from unittest.mock import patch
from models import User, CreditLog
from auth import get_current_user

@pytest.fixture(autouse=True)
def mock_auth(db):
    user = User(
        id="user123",
        email="test_credit@example.com",
        username="Credit User",
        credits=50
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield user
    app.dependency_overrides.clear()

def test_get_credit_packages(client):
    response = client.get("/skin/api/credits/packages")
    assert response.status_code == 200
    packages = response.json()
    assert len(packages) > 0
    assert packages[0]["dollars"] == 1
    assert packages[0]["credits"] == 10

@patch("routers.credit.create_paypal_order_api")
def test_purchase_credits(mock_create_paypal, client):
    mock_create_paypal.return_value = {
        "id": "PAYPAL_ORDER_111",
        "links": [
            {"rel": "approve", "href": "https://paypal.com/approve/111"}
        ]
    }
    payload = {"amount": 5.0}
    response = client.post("/skin/api/credits/purchase", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["paypal_order_id"] == "PAYPAL_ORDER_111"
    assert data["approval_url"] == "https://paypal.com/approve/111"
    assert data["amount"] == 5.0
    assert data["credits"] == 50

@patch("routers.credit.capture_paypal_order_api")
@patch("routers.credit.get_paypal_order_api")
def test_capture_credits_success(mock_get_paypal, mock_capture_paypal, client, db, mock_auth):
    mock_get_paypal.return_value = {
        "status": "APPROVED",
        "purchase_units": [
            {
                "custom_id": "credit:user123:100:random_hash",
                "amount": {"value": "10.00"}
            }
        ]
    }
    mock_capture_paypal.return_value = {
        "status": "COMPLETED",
        "purchase_units": [
            {
                "custom_id": "credit:user123:100:random_hash",
                "amount": {"value": "10.00"}
            }
        ]
    }

    payload = {"paypal_order_id": "PAYPAL_ORDER_111"}
    response = client.post("/skin/api/credits/capture", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["credits_awarded"] == 100
    assert data["new_balance"] == 150

    # Verify log entry in database
    log = db.query(CreditLog).filter(CreditLog.user_id == "user123").first()
    assert log is not None
    assert log.amount == 100
    assert log.action == "purchase"
    assert log.source == "PayPal: PAYPAL_ORDER_111"

@patch("routers.credit.get_paypal_order_api")
def test_capture_credits_invalid_custom_id(mock_get_paypal, client):
    # custom_id doesn't start with "credit:"
    mock_get_paypal.return_value = {
        "status": "APPROVED",
        "purchase_units": [
            {
                "custom_id": "print_order_999",
                "amount": {"value": "10.00"}
            }
        ]
    }

    payload = {"paypal_order_id": "PAYPAL_ORDER_111"}
    response = client.post("/skin/api/credits/capture", json=payload)
    assert response.status_code == 400
    assert "Invalid payment details" in response.json()["detail"]

@patch("routers.credit.get_paypal_order_api")
def test_capture_credits_wrong_user(mock_get_paypal, client):
    # custom_id belongs to user456
    mock_get_paypal.return_value = {
        "status": "APPROVED",
        "purchase_units": [
            {
                "custom_id": "credit:user456:100:random_hash",
                "amount": {"value": "10.00"}
            }
        ]
    }

    payload = {"paypal_order_id": "PAYPAL_ORDER_111"}
    response = client.post("/skin/api/credits/capture", json=payload)
    assert response.status_code == 403
    assert "Payment does not belong to this user" in response.json()["detail"]
