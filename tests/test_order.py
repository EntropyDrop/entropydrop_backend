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
        recipient_name="Test Recipient",
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
    """Test goods_status becomes 'awaiting_review' after print order payment"""
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
    assert order.goods_status == "awaiting_review"

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


    addr = ShippingAddress(recipient_name="Test Recipient", user_id="1", country="US", phone="123456", zip_code="10001", state="NY", city="NYC", detail_address="5th Ave")
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
        recipient_name="Test Recipient",
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
    assert response.status_code == 409
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
        "billing_info": {"last_payment": {"time": "2026-05-25T00:00:00Z", "amount": {"value": "20.00", "currency_code": "USD"}}, "next_billing_time": "2026-06-24T08:30:00Z"},
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
        "billing_info": {"last_payment": {"time": "2026-05-25T00:00:00Z", "amount": {"value": "20.00", "currency_code": "USD"}}, "next_billing_time": "2026-06-24T08:30:00Z"},
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
        "billing_info": {"last_payment": {"time": "2026-05-25T00:00:00Z", "amount": {"value": "20.00", "currency_code": "USD"}}, "next_billing_time": "2026-06-24T08:30:00Z"},
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
        "billing_info": {"last_payment": {"time": "2026-05-25T00:00:00Z", "amount": {"value": "20.00", "currency_code": "USD"}}, "next_billing_time": "2026-06-24T08:30:00Z"},
    }

    response = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-3"})
    assert response.status_code == 200
    user = db.query(User).filter(User.id == "1").first()
    assert user.paypal_subscription_id == "SUB-3"
    assert user.pro_level == "pro-plus"
    assert user.credits == 80

@patch("routers.order.get_paypal_subscription_api")
def test_activate_subscription_deduplication(mock_get_subscription, monkeypatch, client, db):
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_PLUS_PLAN_ID", "P-PLUS")
    monkeypatch.setattr("routers.order.settings.PAYPAL_PRO_MAX_PLAN_ID", "P-MAX")
    mock_get_subscription.return_value = {
        "status": "ACTIVE",
        "plan_id": "P-PLUS",
        "subscriber": {"email_address": "test_order@example.com"},
        "billing_info": {"last_payment": {"time": "2026-05-25T00:00:00Z", "amount": {"value": "20.00", "currency_code": "USD"}}, "next_billing_time": "2026-06-24T08:30:00Z"},
    }

    # 1. First activation
    response1 = client.post("/skin/api/orders/subscription/activate", json={"paypal_order_id": "SUB-3"})
    assert response1.status_code == 200
    
    user = db.query(User).filter(User.id == "1").first()
    assert user.credits == 80

    # 2. Simulate webhook for the same subscription (should be skipped/deduplicated)
    import backend_utils
    backend_utils.award_subscription_credits(db, user, "pro-plus", "SUB-3", is_webhook=True, paid_at=__import__("datetime").datetime(2026, 5, 25, tzinfo=__import__("datetime").timezone.utc))
    db.commit()
    db.refresh(user)
    assert user.credits == 80

    # 3. Simulate webhook for a different subscription (should be granted, e.g. resubscribed)
    backend_utils.award_subscription_credits(db, user, "pro-plus", "SUB-DIFF", is_webhook=True, paid_at=__import__("datetime").datetime(2026, 5, 25, tzinfo=__import__("datetime").timezone.utc))
    db.commit()
    db.refresh(user)
    assert user.credits == 160

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


def seed_cute_figure_kit(db, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[1] / "alembic/versions/f3c72a6b910e_add_cute_figure_kit.py"
    spec = importlib.util.spec_from_file_location("cute_kit_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=db.connection))
    migration.upgrade()
    db.commit()
    from copy import deepcopy
    from models import ModelSalesLimit
    kit = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    kit.kit_specifications = deepcopy(load_kit_spec_migration().CUTE_KIT_SPECIFICATIONS)
    db.commit()
    return migration


def update_cute_figure_kit_price(db, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    path = Path(__file__).resolve().parents[1] / "alembic/versions/b8d62a4f901c_update_cute_kit_price.py"
    spec = importlib.util.spec_from_file_location("cute_kit_price_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_bind=db.connection))
    migration.upgrade()
    db.commit()
    return migration


def test_cute_kit_seed_preserves_existing_inventory_and_prices(db, monkeypatch):
    from models import ModelSalesLimit
    migration = seed_cute_figure_kit(db, monkeypatch)
    kit = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    assert (kit.order_type, kit.price, kit.stock) == ("print", 30, 300)
    kit.stock = 299
    kit.price = 31
    db.commit()
    migration.upgrade()
    db.commit()
    db.refresh(kit)
    assert (kit.price, kit.stock) == (31, 299)
    assert db.query(ModelSalesLimit).filter_by(model_type="PLA+sticker").one().price == 60


@patch("routers.order.clone_skin_for_order")
def test_cute_kit_costs_forty_dollars_with_free_shipping(mock_clone, client, db, monkeypatch):
    from models import GenerationLog, ShippingAddress
    seed_cute_figure_kit(db, monkeypatch)
    update_cute_figure_kit_price(db, monkeypatch)
    response = client.get("/skin/api/orders/model-stock?order_type=print")
    kit = next(item for item in response.json() if item["model_type"] == "Cute DIY Kit")
    assert kit == {"model_type": "Cute DIY Kit", "available": True, "price": 40.0, "stock": 300, "kit_specifications": load_kit_spec_migration().CUTE_KIT_SPECIFICATIONS}
    mock_clone.return_value = "orders/cute/skin.png"
    log = GenerationLog(prompt="Cute test", is_public=True, user_id="1", mode="edit", result="test.png", status="success")
    address = ShippingAddress(recipient_name="Test Recipient", user_id="1", country="US", phone="123456", zip_code="10001", state="NY", city="NYC", detail_address="Test address")
    db.add_all([log, address])
    db.commit()
    payload = {"order_type": "print", "model_type": "Cute DIY Kit", "log_id": log.id, "address_id": address.id}
    response = client.post("/skin/api/orders", json=payload)
    assert response.status_code == 200
    assert (response.json()["price"], response.json()["shipping_fee"], response.json()["total_price"]) == (40, 0, 40)
    assert response.json()["items"][0]["model_type"] == "Cute DIY Kit"
    second = client.post("/skin/api/orders", json=payload)
    assert second.status_code == 200
    assert second.json()["id"] == response.json()["id"]
    assert (second.json()["price"], second.json()["shipping_fee"], second.json()["total_price"]) == (80, 0, 80)


@pytest.mark.parametrize("status", ["pending_payment", "paid"])
def test_cute_price_update_preserves_stock_other_products_and_existing_orders(db, monkeypatch, status):
    from models import ModelSalesLimit
    seed_cute_figure_kit(db, monkeypatch)
    kit = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    kit.stock = 287
    order = Order(user_id="1", order_type="print", status=status, price=30, shipping_fee=0, total_price=30)
    db.add(order)
    db.flush()
    item = OrderItem(order_id=order.id, model_type="Cute DIY Kit", price=30, refer_log_id="previous-skin")
    db.add(item)
    db.commit()
    migration = update_cute_figure_kit_price(db, monkeypatch)
    migration.upgrade()
    migration.downgrade()
    db.commit()
    db.refresh(kit)
    db.refresh(order)
    db.refresh(item)
    assert (kit.price, kit.stock) == (40, 287)
    assert db.query(ModelSalesLimit).filter_by(model_type="PLA+sticker").one().price == 60
    assert db.query(ModelSalesLimit).filter_by(model_type="pro_1m").one().price == 10
    assert (order.price, order.shipping_fee, order.total_price, item.price) == (30, 0, 30, 30)
    assert order.status == status


@pytest.fixture()
def quantity_order(db, monkeypatch):
    from models import GenerationLog, ShippingAddress
    seed_cute_figure_kit(db, monkeypatch)
    update_cute_figure_kit_price(db, monkeypatch)
    log = GenerationLog(prompt="Quantity test", is_public=True, user_id="1", mode="edit", result="test.png", status="success")
    address = ShippingAddress(recipient_name="Test Recipient", user_id="1", country="US", phone="123456", zip_code="10001", state="NY", city="NYC", detail_address="Test address")
    db.add_all([log, address])
    db.commit()
    monkeypatch.setattr("routers.order.clone_skin_for_order", lambda log, order_id, item_id: f"orders/{order_id}/{item_id}.png")
    return {"order_type": "print", "model_type": "Cute DIY Kit", "log_id": log.id, "address_id": address.id}


def test_multiple_kits_have_correct_total_snapshots_and_merged_quantity(client, db, quantity_order):
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 3})
    assert response.status_code == 200
    order = response.json()
    assert (order["price"], order["shipping_fee"], order["total_price"]) == (120, 0, 120)
    assert len(order["items"]) == 3
    assert len({item["id"] for item in order["items"]}) == 3
    assert all(item["refer_log_id"] == quantity_order["log_id"] and item["price"] == 40 for item in order["items"])
    stored = db.query(OrderItem).filter_by(order_id=order["id"]).all()
    assert len({item.skin_url for item in stored}) == 3
    assert all(item.source_snapshot["skin_id"] == quantity_order["log_id"] for item in stored)
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 2})
    assert response.status_code == 200
    assert response.json()["id"] == order["id"]
    assert len(response.json()["items"]) == 5
    assert response.json()["total_price"] == 200
    detail = client.get(f"/skin/api/orders/{order['id']}").json()
    assert all(item["refer_log_id"] == quantity_order["log_id"] for item in detail["items"])
    item_id = detail["items"][-1]["id"]
    with patch("routers.order.s3_client.delete_object"):
        assert client.delete(f"/skin/api/orders/items/{item_id}").status_code == 200
    detail = client.get(f"/skin/api/orders/{order['id']}").json()
    assert (len(detail["items"]), detail["total_price"]) == (4, 160)


@pytest.mark.parametrize("quantity", [0, -1, 11, 1.5, "2", True, None])
def test_invalid_kit_quantity_is_rejected_without_creating_orders(client, db, quantity_order, quantity):
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": quantity})
    assert response.status_code == 422
    assert db.query(Order).count() == 0
    assert db.query(OrderItem).count() == 0


def test_quantity_cannot_exceed_unpaid_order_limit(client, db, quantity_order):
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 9})
    assert response.status_code == 200
    order_id = response.json()["id"]
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 2})
    assert response.status_code == 400
    order = client.get(f"/skin/api/orders/{order_id}").json()
    assert (len(order["items"]), order["total_price"]) == (9, 360)


def test_quantity_cannot_exceed_stock_even_after_merging(client, db, quantity_order):
    from models import ModelSalesLimit
    kit = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    kit.stock = 3
    db.commit()
    assert client.post("/skin/api/orders", json={**quantity_order, "quantity": 4}).status_code == 400
    assert db.query(Order).count() == 0
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 2})
    assert response.status_code == 200
    order_id = response.json()["id"]
    assert client.post("/skin/api/orders", json={**quantity_order, "quantity": 2}).status_code == 400
    order = client.get(f"/skin/api/orders/{order_id}").json()
    assert (len(order["items"]), order["total_price"]) == (2, 80)


def test_subscription_quantity_remains_one(client, db):
    response = client.post("/skin/api/orders", json={"order_type": "subscription", "model_type": "pro_1m", "quantity": 2})
    assert response.status_code == 400
    assert db.query(Order).count() == 0



def test_quantity_merge_releases_own_hold_without_double_counting_stock(client, db, quantity_order):
    from models import ModelSalesLimit
    from order_inventory import reserve_inventory
    kit = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    kit.stock = 3
    db.commit()
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 2})
    assert response.status_code == 200
    order = db.query(Order).filter_by(id=response.json()["id"]).one()
    reserve_inventory(db, order)
    db.commit()
    db.refresh(kit)
    assert kit.stock == 1
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 1})
    assert response.status_code == 200
    assert (len(response.json()["items"]), response.json()["total_price"]) == (3, 120)
    db.refresh(kit)
    db.refresh(order)
    assert kit.stock == 3
    assert order.inventory_reserved is False
    reserve_inventory(db, order)
    db.commit()
    db.refresh(kit)
    assert kit.stock == 0



def load_kit_spec_migration():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "alembic/versions/c7a31d902ef4_add_kit_specifications.py"
    spec = importlib.util.spec_from_file_location("kit_specifications_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_order_specifications_are_server_snapshots_for_every_kit(client, db, quantity_order):
    from copy import deepcopy
    from models import ModelSalesLimit
    cfg = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    original = deepcopy(cfg.kit_specifications)
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 2, "kit_specifications_snapshot": {"product_name": "Forged"}})
    assert response.status_code == 200
    order = response.json()
    assert all(item["kit_specifications_snapshot"] == original for item in order["items"])
    assert all(item["kit_specifications_current"] is None for item in order["items"])
    stored = db.query(OrderItem).filter_by(order_id=order["id"]).all()
    assert len(stored) == 2 and all(item.kit_specifications_snapshot == original for item in stored)
    revised = deepcopy(original)
    revised.update(product_name="CUTE replacement edition", dimensions="Approx. 8 × 5 × 3 cm")
    revised["materials"][-1]["quantity"] = 4
    cfg.kit_specifications = revised
    db.commit()
    response = client.post("/skin/api/orders", json={**quantity_order, "quantity": 1})
    assert response.status_code == 200 and response.json()["id"] == order["id"]
    for url in [f"/skin/api/orders/{order['id']}", "/skin/api/orders"]:
        data = client.get(url).json()
        if url == "/skin/api/orders":
            data = data["items"][0]
        items = data["items"]
        assert len(items) == 3
        assert sum(item["kit_specifications_snapshot"] == original for item in items) == 2
        assert sum(item["kit_specifications_snapshot"] == revised for item in items) == 1
    # Snapshot readback does not require the catalog row to remain available.
    db.delete(cfg); db.commit()
    items = client.get(f"/skin/api/orders/{order['id']}").json()["items"]
    assert all(item["kit_specifications_snapshot"] for item in items)


def test_old_kit_orders_label_current_catalog_separately_and_admin_reads_same_data(client, db, quantity_order):
    from models import ModelSalesLimit
    from main import app
    from auth import get_current_admin
    order = Order(user_id="1", order_type="print", status="paid", price=30, shipping_fee=0, total_price=30)
    db.add(order); db.flush()
    db.add(OrderItem(order_id=order.id, model_type="Cute DIY Kit", price=30))
    db.commit()
    cfg = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    app.dependency_overrides[get_current_admin] = lambda: db.query(User).filter_by(id="1").one()
    for url in [f"/skin/api/orders/{order.id}", f"/api/figure/orders?stage=all&order_id={order.id}"]:
        data = client.get(url).json()
        if url.startswith("/api/figure"):
            data = data["items"][0]
        assert data["items"][0]["kit_specifications_snapshot"] is None
        assert data["items"][0]["kit_specifications_current"] == cfg.kit_specifications
    assert db.query(OrderItem).filter_by(order_id=order.id).one().kit_specifications_snapshot is None


def test_cute_order_requires_catalog_specifications(client, db, quantity_order):
    from models import ModelSalesLimit
    cfg = db.query(ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    cfg.kit_specifications = None
    db.commit()
    response = client.post("/skin/api/orders", json=quantity_order)
    assert response.status_code == 409
    assert db.query(Order).count() == 0


def test_kit_spec_migration_preserves_prices_stock_and_historical_orders(monkeypatch):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE model_sales_limits (model_type VARCHAR(100), order_type VARCHAR(20), price FLOAT, stock INTEGER)"))
        connection.execute(sa.text("CREATE TABLE order_items (id VARCHAR(16), model_type VARCHAR(100), price FLOAT)"))
        connection.execute(sa.text("INSERT INTO model_sales_limits VALUES ('Cute DIY Kit', 'print', 41, 285), ('Other model', 'print', 60, 80)"))
        connection.execute(sa.text("INSERT INTO order_items VALUES ('old-kit', 'Cute DIY Kit', 30)"))
        migration = load_kit_spec_migration()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        products = sa.Table("model_sales_limits", sa.MetaData(), autoload_with=connection)
        rows = connection.execute(sa.select(products)).mappings().all()
        assert (rows[0]["price"], rows[0]["stock"]) == (41, 285)
        assert rows[0]["kit_specifications"] == migration.CUTE_KIT_SPECIFICATIONS
        assert rows[1]["kit_specifications"] is None
        row = connection.execute(sa.text("SELECT price, kit_specifications_snapshot FROM order_items")).one()
        assert tuple(row) == (30, None)
        migration.downgrade()
        assert "kit_specifications" not in {column["name"] for column in sa.inspect(connection).get_columns("model_sales_limits")}
    engine.dispose()


def test_order_keeps_recipient_snapshot_after_address_edit_and_deletion(client, db, quantity_order):
    from models import ShippingAddress
    response = client.post('/api/orders', json=quantity_order)
    assert response.status_code == 200
    order_id = response.json()['id']
    assert response.json()['address']['recipient_name'] == 'Test Recipient'
    saved = db.query(ShippingAddress).filter_by(id=quantity_order['address_id']).one()
    saved.recipient_name = 'Changed Recipient'
    db.commit()
    assert client.get(f'/api/orders/{order_id}').json()['address']['recipient_name'] == 'Test Recipient'
    db.delete(saved)
    db.commit()
    assert client.get(f'/api/orders/{order_id}').json()['address']['recipient_name'] == 'Test Recipient'
