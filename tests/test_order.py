import pytest
from models import User, Order, OrderItem
from auth import get_current_user
import uuid

pytestmark = pytest.mark.usefixtures("mock_auth")

@pytest.fixture()
def mock_auth(db):
    user = User(
        id="1",
        email="test_order@example.com",
        username="Order User"
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    
    from models import ModelSalesLimit
    # Seed items for dynamic pricing
    db.add_all([
        ModelSalesLimit(model_type="10cm Model V1", stock=100, price=60.0),
        ModelSalesLimit(model_type="pro_1m", stock=1000, price=10.0, order_type="subscription"),
        ModelSalesLimit(model_type="pro_3m", stock=1000, price=25.0, order_type="subscription"),
        ModelSalesLimit(model_type="pro_6m", stock=1000, price=45.0, order_type="subscription"),
        ModelSalesLimit(model_type="pro_1y", stock=1000, price=80.0, order_type="subscription"),
        ModelSalesLimit(model_type="PLA+sticker", stock=100, price=60.0),
    ])
    db.commit()

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()

def test_create_order_empty(client, db):
    response = client.post("/skin/api/orders", json={
        "order_type": "print"
    })
    assert response.status_code in [200, 400]

def test_get_orders(client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        price=10.0,
        shipping_fee=5.0,
        total_price=15.0
    )
    db.add(order)
    db.commit()

    response = client.get("/skin/api/orders")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert any(o["price"] == 10.0 for o in data["items"])

def test_cancel_order(client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        price=10.0,
        shipping_fee=5.0,
        total_price=15.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    response = client.put(f"/skin/api/orders/{order.id}/cancel")
    assert response.status_code in [200, 400]
    
    db.refresh(order)
    assert order.status == "cancelled"

def test_cancel_order_idempotent(client, db):
    order = Order(
        user_id="1",
        status="cancelled",
        price=10.0,
        shipping_fee=5.0,
        total_price=15.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    response = client.put(f"/skin/api/orders/{order.id}/cancel")
    assert response.status_code == 200
    
    db.refresh(order)
    assert order.status == "cancelled"

def test_delete_order(client, db):
    order = Order(
        user_id="1",
        status="cancelled",
        price=10.0,
        shipping_fee=5.0,
        total_price=15.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    response = client.delete(f"/skin/api/orders/{order.id}")
    assert response.status_code == 200

    deleted_order = db.query(Order).filter(Order.id == order.id).first()
    assert deleted_order is None

def test_get_order_detail(client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        price=20.0,
        shipping_fee=0.0,
        total_price=20.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    response = client.get(f"/skin/api/orders/{order.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == order.id
    assert data["price"] == 20.0

def test_get_order_not_found(client, db):
    response = client.get("/skin/api/orders/invalid_id")
    assert response.status_code == 404

def test_pay_order_requires_paypal_order_id(client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={})
    assert response.status_code == 400
    
    db.refresh(order)
    assert order.status == "pending_payment"
    assert order.paid_at is None

def test_delete_order_item(client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    item = OrderItem(
        order_id=order.id,
        model_type="PLA+sticker",
        price=60.0
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    response = client.delete(f"/skin/api/orders/items/{item.id}")
    assert response.status_code == 200
    
    db.refresh(order)
    assert order.price == 0.0
    assert order.status == "cancelled" # Since remaining items is 0

def test_get_paypal_client_id(client):
    response = client.get("/skin/api/orders/paypal/config")
    assert response.status_code == 200
    data = response.json()
    assert "client_id" in data

# ----------------- More API Tests (Coverage) -----------------

from unittest.mock import patch

def paypal_order_payload(order, status="APPROVED", amount=None, custom_id=None, currency="USD"):
    return {
        "id": order.paypal_order_id,
        "status": status,
        "purchase_units": [
            {
                "custom_id": custom_id or order.id,
                "amount": {
                    "currency_code": currency,
                    "value": f"{amount if amount is not None else order.total_price:.2f}",
                },
            }
        ],
    }


def paypal_capture_payload(order, amount=None, custom_id=None, currency="USD", capture_status="COMPLETED"):
    return {
        "id": order.paypal_order_id,
        "status": "COMPLETED",
        "purchase_units": [
            {
                "custom_id": custom_id or order.id,
                "payments": {
                    "captures": [
                        {
                            "status": capture_status,
                            "amount": {
                                "currency_code": currency,
                                "value": f"{amount if amount is not None else order.total_price:.2f}",
                            },
                        }
                    ]
                },
            }
        ],
    }

def test_create_order_subscription(client, db):
    """Test creating subscription order"""
    payload = {
        "order_type": "subscription",
        "model_type": "pro_1m"
    }
    response = client.post("/skin/api/orders", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["order_type"] == "subscription"
    assert data["price"] == 10.0

@patch("routers.order.clone_skin_for_order")
def test_create_order_print_with_log(mock_clone, client, db):
    """Test print order with GenerationLog"""
    mock_clone.return_value = "orders/fake_order/fake_item.png"
    
    from models import GenerationLog, ShippingAddress
    log = GenerationLog(prompt="test", is_public=True, user_id="1", mode="edit", result="res.png", status="success")
    db.add(log)
    addr = ShippingAddress(
        user_id="1", 
        country="US", 
        phone="123456", 
        zip_code="10001", 
        state="NY", 
        city="NYC", 
        detail_address="5th Ave"
    )
    db.add(addr)
    db.commit()

    payload = {
        "order_type": "print",
        "log_id": log.id,
        "address_id": addr.id,
        "model_type": "10cm Model V1"
    }
    
    response = client.post("/skin/api/orders", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["order_type"] == "print"
    assert data["price"] == 60.0
    assert len(data["items"]) == 1

def test_create_order_invalid_address(client, db):
    """Test creating order with invalid shipping address should fail"""
    payload = {
        "order_type": "print",
        "address_id": "9999", # String type for Pydantic validation
        "model_type": "PLA+sticker"
    }
    response = client.post("/skin/api/orders", json=payload)
    assert response.status_code == 400

@patch("routers.order.s3_client.copy_object")
def test_clone_skin_for_order(mock_copy, db):
    """Test cloning image for order"""
    from routers.order import clone_skin_for_order
    from models import GenerationLog
    log_entry = GenerationLog(prompt="test", is_public=True, result="file.png")
    
    res = clone_skin_for_order(log_entry, "order_123", "item_456")
    assert res == "orders/order_123/item_456.png"
    mock_copy.assert_called_once()

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_paypal_approved(mock_capture, mock_get_order, client, db):
    """Test capture success and pay when PayPal order status is APPROVED"""
    from models import Order
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Mock binding
    order.paypal_order_id = "paypal_123"
    db.commit()

    mock_get_order.return_value = paypal_order_payload(order, "APPROVED")
    mock_capture.return_value = paypal_capture_payload(order)

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_123"})
    assert response.status_code == 200
    
    db.refresh(order)
    assert order.status == "paid"
    mock_get_order.assert_called_once_with("paypal_123")
    mock_capture.assert_called_once_with("paypal_123")

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_paypal_completed(mock_capture, mock_get_order, client, db):
    """Test skipping capture and paying directly when PayPal order status is COMPLETED"""
    from models import Order
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Mock binding
    order.paypal_order_id = "paypal_123"
    db.commit()

    mock_get_order.return_value = paypal_order_payload(order, "COMPLETED")

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_123"})
    assert response.status_code == 200
    
    db.refresh(order)
    assert order.status == "paid"
    mock_get_order.assert_called_once_with("paypal_123")
    mock_capture.assert_not_called()

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_paypal_mismatched(mock_capture, mock_get_order, client, db):
    """Test error 400 when PayPal order credentials do not match"""
    from models import Order
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0,
        paypal_order_id="paypal_correct"
    )
    db.add(order)
    db.commit()

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_wrong"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Payment voucher mismatch"

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_rejects_paypal_amount_mismatch(mock_capture, mock_get_order, client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0,
        paypal_order_id="paypal_amount_bad"
    )
    db.add(order)
    db.commit()

    mock_get_order.return_value = paypal_order_payload(order, "APPROVED", amount=1.00)

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_amount_bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "PayPal amount mismatch"
    db.refresh(order)
    assert order.status == "pending_payment"
    mock_capture.assert_not_called()

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_rejects_paypal_custom_id_mismatch(mock_capture, mock_get_order, client, db):
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0,
        paypal_order_id="paypal_custom_bad"
    )
    db.add(order)
    db.commit()

    mock_get_order.return_value = paypal_order_payload(order, "APPROVED", custom_id="other_order")

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_custom_bad"})
    assert response.status_code == 400
    assert response.json()["detail"] == "PayPal order binding mismatch"
    db.refresh(order)
    assert order.status == "pending_payment"
    mock_capture.assert_not_called()

@patch("routers.order.get_paypal_order_api")
@patch("routers.order.capture_paypal_order_api")
def test_pay_order_print_goods_status(mock_capture, mock_get_order, client, db):
    """Test goods_status becomes 'preparing' after print order payment"""
    from models import Order
    order = Order(
        user_id="1",
        status="pending_payment",
        order_type="print",
        price=60.0,
        shipping_fee=0.0,
        total_price=60.0,
        paypal_order_id="paypal_print_123"
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    mock_get_order.return_value = paypal_order_payload(order, "APPROVED")
    mock_capture.return_value = paypal_capture_payload(order)

    response = client.post(f"/skin/api/orders/{order.id}/pay", json={"paypal_order_id": "paypal_print_123"})
    assert response.status_code == 200
    
    db.refresh(order)
    assert order.status == "paid"
    assert order.goods_status == "preparing"

def test_get_model_stock(client, db):
    """Test getting model stock status"""
    from models import ModelSalesLimit
    limit = db.query(ModelSalesLimit).filter(ModelSalesLimit.model_type == "10cm Model V1").first()
    limit.stock = 5
    db.commit()


    response = client.get("/skin/api/orders/model-stock")
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0
    item = next(i for i in data if i["model_type"] == "10cm Model V1")
    assert item["available"] is True

def test_create_order_limit_reached(client, db):
    """Test order creation fails when sales limit is reached"""
    from models import ModelSalesLimit, ShippingAddress
    limit = db.query(ModelSalesLimit).filter(ModelSalesLimit.model_type == "10cm Model V1").first()
    limit.stock = 0
    db.commit()


    addr = ShippingAddress(user_id="1", country="US", phone="123456", zip_code="10001", state="NY", city="NYC", detail_address="5th Ave")
    db.add(addr)
    db.commit()

    payload = {
        "order_type": "print",
        "address_id": addr.id,
        "model_type": "10cm Model V1"
    }
    
    response = client.post("/skin/api/orders", json=payload)
    assert response.status_code == 400
    assert "sold out" in response.json()["detail"]

@patch("routers.order.clone_skin_for_order")
def test_adding_item_clears_existing_paypal_order_id(mock_clone, client, db):
    from models import GenerationLog, ShippingAddress

    mock_clone.return_value = "orders/fake_order/fake_item.png"
    log1 = GenerationLog(prompt="test 1", is_public=True, user_id="1", mode="edit", result="res1.png", status="success")
    log2 = GenerationLog(prompt="test 2", is_public=True, user_id="1", mode="edit", result="res2.png", status="success")
    addr = ShippingAddress(
        user_id="1",
        country="US",
        phone="123456",
        zip_code="10001",
        state="NY",
        city="NYC",
        detail_address="5th Ave"
    )
    db.add_all([log1, log2, addr])
    db.commit()

    first = client.post("/skin/api/orders", json={
        "order_type": "print",
        "log_id": log1.id,
        "address_id": addr.id,
        "model_type": "10cm Model V1"
    })
    assert first.status_code == 200
    order = db.query(Order).filter(Order.id == first.json()["id"]).first()
    order.paypal_order_id = "paypal_stale"
    db.commit()

    second = client.post("/skin/api/orders", json={
        "order_type": "print",
        "log_id": log2.id,
        "address_id": addr.id,
        "model_type": "10cm Model V1"
    })
    assert second.status_code == 200
    db.refresh(order)
    assert order.total_price == 120.0
    assert order.paypal_order_id is None

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_rejects_non_active_subscription(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "APPROVAL_PENDING",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": "test_order@example.com"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-1"})
    assert response.status_code == 400
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id is None

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_rejects_other_user_email(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": "someone_else@example.com"},
        "billing_info": {"next_billing_time": "2026-06-24T08:30:00Z"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-2"})
    assert response.status_code == 403
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id is None

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_accepts_matching_custom_id_with_different_paypal_email(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "custom_id": "1",
        "subscriber": {"email_address": "paypal_buyer@example.com"},
        "billing_info": {"next_billing_time": "2026-06-24T08:30:00Z"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-CUSTOM"})
    assert response.status_code == 200
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id == "SUB-CUSTOM"
    assert user.pro_level == "pro-plus"

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_rejects_mismatched_custom_id(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "custom_id": "OTHERUSER000000",
        "subscriber": {"email_address": "test_order@example.com"},
        "billing_info": {"next_billing_time": "2026-06-24T08:30:00Z"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-CUSTOM-BAD"})
    assert response.status_code == 403
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id is None

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_success(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": "test_order@example.com"},
        "billing_info": {"next_billing_time": "2026-06-24T08:30:00Z"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-3"})
    assert response.status_code == 200
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id == "SUB-3"
    assert user.pro_level == "pro-plus"
    assert user.credits == 60

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_deduplication(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": "test_order@example.com"},
        "billing_info": {"next_billing_time": "2026-06-24T08:30:00Z"},
    }

    # 1. First activation
    response1 = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-3"})
    assert response1.status_code == 200
    
    user = db.query(User).filter(User.id == "1").first()
    assert user.credits == 60

    # 2. Simulate webhook for the same subscription (should be skipped/deduplicated)
    import backend_utils
    backend_utils.award_subscription_credits(db, user, "pro-plus", "SUB-3", is_webhook=True)
    db.commit()
    db.refresh(user)
    assert user.credits == 60

    # 3. Simulate webhook for a different subscription (should be granted, e.g. resubscribed)
    backend_utils.award_subscription_credits(db, user, "pro-plus", "SUB-DIFF", is_webhook=True)
    db.commit()
    db.refresh(user)
    assert user.credits == 120

def test_activate_subscription_rejects_subscription_linked_to_other_user(client, db):
    other = User(
        id="OTHERUSER000000",
        email="other@example.com",
        username="Other User",
        paypal_subscription_id="SUB-4",
    )
    db.add(other)
    db.commit()

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-4"})
    assert response.status_code == 403
