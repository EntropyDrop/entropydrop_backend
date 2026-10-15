"""Buy Now must never charge items from an existing or subsequently edited cart."""
from pathlib import Path
from unittest.mock import Mock
import importlib.util

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from test_order import mock_auth, quantity_order

pytestmark = pytest.mark.usefixtures("mock_auth")


@pytest.mark.parametrize("shipping_source", ["address_book", "paypal"])
def test_direct_purchase_is_separate_from_cart_before_and_after_checkout(client, db, quantity_order, monkeypatch, shipping_source):
    request = {**quantity_order, "shipping_source": shipping_source}
    if shipping_source == "paypal":
        request.pop("address_id")
    cart = client.post("/api/orders", json={**request, "quantity": 2}).json()
    direct_response = client.post("/api/orders", json={**request, "quantity": 3, "checkout_mode": "buy_now"})
    assert direct_response.status_code == 200, direct_response.text
    direct = direct_response.json()
    assert direct["id"] != cart["id"]
    assert direct["checkout_mode"] == "buy_now" and cart["checkout_mode"] == "cart"
    assert len(direct["items"]) == 3 and direct["total_price"] == 120
    create = Mock(return_value={"id": "DIRECT-PAYPAL", "status": "CREATED"})
    monkeypatch.setattr("routers.order.create_paypal_order_api", create)
    assert client.post(f"/api/orders/{direct['id']}/create-paypal-order").status_code == 200
    assert create.call_args.args == (120, direct["id"])
    added = client.post("/api/orders", json=request).json()
    assert added["id"] == cart["id"] and len(added["items"]) == 3
    unchanged = client.get(f"/api/orders/{direct['id']}").json()
    assert len(unchanged["items"]) == 3 and unchanged["total_price"] == 120
    stored = db.query(models.Order).filter_by(id=direct["id"]).one()
    db.refresh(stored)
    assert stored.paypal_order_id == "DIRECT-PAYPAL" and stored.inventory_reserved


def test_cart_creation_does_not_reuse_an_earlier_direct_purchase(client, db, quantity_order):
    direct = client.post("/api/orders", json={**quantity_order, "checkout_mode": "buy_now"}).json()
    cart = client.post("/api/orders", json=quantity_order).json()
    assert direct["id"] != cart["id"]
    second_direct = client.post("/api/orders", json={**quantity_order, "checkout_mode": "buy_now"}).json()
    assert second_direct["id"] not in (cart["id"], direct["id"])
    assert db.query(models.Order).count() == 3
    assert all(order.total_price == 40 for order in db.query(models.Order))


def test_invalid_mode_and_incomplete_address_cannot_create_a_direct_purchase(client, db, quantity_order):
    assert client.post("/api/orders", json={**quantity_order, "checkout_mode": "unknown"}).status_code == 422
    assert client.post("/api/orders", json={**quantity_order, "address_id": None, "checkout_mode": "buy_now"}).status_code == 400
    assert db.query(models.Order).count() == 0


def test_checkout_mode_migration_preserves_old_carts(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/c6a841f2309d_add_order_checkout_mode.py"
    spec = importlib.util.spec_from_file_location("checkout_mode_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orders (id VARCHAR(16), total_price FLOAT)"))
        connection.execute(sa.text("INSERT INTO orders VALUES ('old-order', 80)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        row = connection.execute(sa.text("SELECT total_price, checkout_mode FROM orders")).one()
        assert tuple(row) == (80, "cart")
        migration.downgrade()
        assert "checkout_mode" not in {column["name"] for column in sa.inspect(connection).get_columns("orders")}
    engine.dispose()
