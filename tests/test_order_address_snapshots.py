"""Order shipping snapshots stay independent of mutable address-book entries."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

import auth
import models
from main import app
from shipping_address_rules import ADDRESS_FIELDS
from test_order import mock_auth, quantity_order

pytestmark = pytest.mark.usefixtures("mock_auth")


@pytest.mark.parametrize("changes", [
    {"recipient_name": "New Recipient"},
    {"country": "CA", "state": "ON", "zip_code": "M5V 2T6"},
    {"state": "CA"},
    {"city": "Albany"},
    {"zip_code": "10002"},
    {"detail_address": "A different street"},
    {"phone": "+1 555 0200"},
], ids=["recipient", "country", "state", "city", "postal", "street", "phone"])
def test_changed_shipping_creates_separate_order_and_preserves_original_checkout(client, db, quantity_order, monkeypatch, changes):
    response = client.post("/api/orders", json=quantity_order)
    assert response.status_code == 200, response.text
    original = response.json()
    order_id = original["id"]
    create_payment = Mock(return_value={"id": "ORIGINAL-PAYPAL", "status": "CREATED"})
    monkeypatch.setattr("routers.order.create_paypal_order_api", create_payment)
    assert client.post(f"/api/orders/{order_id}/create-paypal-order").status_code == 200
    stored = db.query(models.Order).filter_by(id=order_id).one()
    db.refresh(stored)
    reserved_until = stored.inventory_reserved_until

    updated = client.put(f"/api/addresses/{quantity_order['address_id']}", json=changes)
    assert updated.status_code == 200, updated.text
    added = client.post("/api/orders", json=quantity_order)
    assert added.status_code == 200, added.text
    new = added.json()
    assert new["id"] != order_id
    assert new["address"] == updated.json()
    assert len(new["items"]) == 1 and new["total_price"] == 40

    db.refresh(stored)
    assert stored.address_snapshot == original["address"]
    assert stored.total_price == 40
    assert stored.paypal_order_id == "ORIGINAL-PAYPAL"
    assert stored.inventory_reserved and stored.inventory_reserved_until == reserved_until
    assert db.query(models.OrderItem).filter_by(order_id=order_id).count() == 1

    # There are now two unpaid orders with the same address ID: select the
    # matching snapshot, not whichever order happens to be returned first.
    again = client.post("/api/orders", json=quantity_order)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == new["id"]
    assert len(again.json()["items"]) == 2 and again.json()["total_price"] == 80
    assert db.query(models.Order).count() == 2
    assert client.get(f"/api/orders/{order_id}").json()["address"] == original["address"]


def test_default_address_change_does_not_replace_snapshot_or_prevent_merging(client, db, quantity_order):
    original = client.post("/api/orders", json=quantity_order).json()
    response = client.put(f"/api/addresses/{quantity_order['address_id']}", json={"is_default": True})
    assert response.status_code == 200
    merged = client.post("/api/orders", json=quantity_order)
    assert merged.status_code == 200
    assert merged.json()["id"] == original["id"]
    assert merged.json()["address"] == original["address"]
    assert merged.json()["address"]["is_default"] is False
    assert len(merged.json()["items"]) == 2


@pytest.mark.parametrize("missing", ["snapshot", "recipient"])
def test_adding_a_kit_does_not_overwrite_incomplete_legacy_snapshots(client, db, quantity_order, missing):
    original = client.post("/api/orders", json=quantity_order).json()
    order = db.query(models.Order).filter_by(id=original["id"]).one()
    snapshot = None if missing == "snapshot" else {key: value for key, value in order.address_snapshot.items() if key != "recipient_name"}
    order.address_snapshot = deepcopy(snapshot)
    db.commit()
    added = client.post("/api/orders", json=quantity_order)
    assert added.status_code == 200
    assert added.json()["id"] != order.id
    db.refresh(order)
    assert order.address_snapshot == snapshot
    assert order.total_price == 40


@pytest.mark.parametrize("status", ["pending_payment", "paid", "shipping", "completed"])
def test_address_edit_and_delete_preserve_order_views_and_payment(client, db, quantity_order, monkeypatch, status):
    original = client.post("/api/orders", json=quantity_order).json()
    order_id = original["id"]
    stored = db.query(models.Order).filter_by(id=order_id).one()
    stored.status = status
    db.commit()
    address_url = f"/api/addresses/{quantity_order['address_id']}"
    changed = {"recipient_name": "Changed Recipient", "country": "CA", "state": "ON", "city": "Toronto", "zip_code": "M5V 2T6", "detail_address": "A different street", "phone": "+1 555 0200"}
    assert set(changed) == set(ADDRESS_FIELDS)
    assert client.put(address_url, json=changed).status_code == 200
    assert client.get(f"/api/orders/{order_id}").json()["address"] == original["address"]
    assert client.delete(address_url).status_code == 200
    assert db.query(models.ShippingAddress).filter_by(id=quantity_order["address_id"]).first() is None
    assert client.get(f"/api/orders/{order_id}").json()["address"] == original["address"]
    orders = client.get("/api/orders").json()["items"]
    assert next(order for order in orders if order["id"] == order_id)["address"] == original["address"]
    app.dependency_overrides[auth.get_current_admin] = lambda: db.query(models.User).filter_by(id="1").one()
    admin = client.get("/api/figure/orders", params={"stage": "all", "order_id": order_id})
    assert admin.status_code == 200
    assert admin.json()["items"][0]["address"] == original["address"]
    assert client.post("/api/orders", json=quantity_order).status_code == 400
    if status == "pending_payment":
        create_payment = Mock(return_value={"id": "PAYPAL-AFTER-DELETE", "status": "CREATED"})
        monkeypatch.setattr("routers.order.create_paypal_order_api", create_payment)
        checkout = client.post(f"/api/orders/{order_id}/create-paypal-order")
        assert checkout.status_code == 200, checkout.text
        create_payment.assert_called_once_with(40, order_id,
            shipping_name="Test Recipient", shipping_address={"country_code": "US", "admin_area_1": "NY", "admin_area_2": "NYC", "address_line_1": "Test address", "postal_code": "10001"})
    db.refresh(stored)
    assert stored.address_snapshot == original["address"] and stored.status == status
