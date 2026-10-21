"""Shipping configuration, quote validation and purchase-time fee snapshots."""
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from config import settings
from test_order import mock_auth, quantity_order
from test_creator_referrals import codes

pytestmark = pytest.mark.usefixtures('mock_auth')
ENDPOINT = '/api/figure/models/shipping'


def test_shipping_settings_require_admin_validate_amount_and_detect_conflicts(client, db, monkeypatch):
    db.query(models.FigureShipping).delete(); db.commit()
    assert client.get(ENDPOINT).status_code == 403
    assert client.patch(ENDPOINT, json={'shipping_fee': 5, 'expected_version': 0}).status_code == 403
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', 'test_order@example.com')
    assert client.get(ENDPOINT).json() == {'shipping_fee': 12.9, 'version': 0}
    for amount in [-1, 1.234, True, 'NaN', 'Infinity', 1000000]:
        assert client.patch(ENDPOINT, json={'shipping_fee': amount, 'expected_version': 0}).status_code == 422
    assert client.patch(ENDPOINT, json={'shipping_fee': 12.9, 'expected_version': 0}).json() == {'shipping_fee': 12.9, 'version': 1}
    assert client.patch(ENDPOINT, json={'shipping_fee': 15, 'expected_version': 0}).status_code == 409
    assert client.patch(ENDPOINT, json={'shipping_fee': 0, 'expected_version': 1}).json() == {'shipping_fee': 0, 'version': 2}
    assert client.patch(ENDPOINT, json={'shipping_fee': 15, 'expected_version': 1}).status_code == 409
    assert client.get(ENDPOINT).json() == {'shipping_fee': 0, 'version': 2}


def test_default_quote_and_payment_keep_shipping_outside_stacked_discounts(client, db, quantity_order, codes, monkeypatch):
    db.query(models.FigureShipping).delete()
    db.add(models.FigureDiscount(id=1, discount_bps=5000, version=1)); db.commit()
    catalog = client.get('/api/orders/model-stock').json()
    assert next(p for p in catalog if p['model_type'] == 'Cute DIY Kit')['shipping_fee'] == 12.9
    assert next(p for p in catalog if p['model_type'] == 'pro_1m')['shipping_fee'] == 0
    quote = client.post('/api/creators/quote', json={'model_type': 'Cute DIY Kit', 'quantity': 2, 'code': 'ALPHA'}).json()
    assert (quote['shipping_fee'], quote['discount_amount'], quote['total_price']) == (12.9, 33, 39.9)
    response = client.post('/api/orders', json={**quantity_order, 'quantity': 2, 'creator_code': 'ALPHA', 'expected_shipping_fee': 12.9})
    assert response.status_code == 200, response.text
    order = response.json()
    assert (order['shipping_fee'], order['total_price']) == (12.9, 39.9)
    assert db.get(models.Order, order['id']).creator_commission == 4.05
    db.add(models.FigureShipping(id=1, shipping_fee=20, version=1)); db.commit()
    # Even after a fee change, payment uses the saved price the customer accepted.
    checkout = Mock(return_value={'id': 'SHIPPING-PAYPAL', 'status': 'CREATED'})
    monkeypatch.setattr('routers.order.create_paypal_order_api', checkout)
    assert client.post(f"/api/orders/{order['id']}/create-paypal-order").status_code == 200
    assert checkout.call_args.args == (39.9, order['id'])
    rejected = client.post('/api/orders', json={**quantity_order, 'expected_shipping_fee': 12.9})
    assert rejected.status_code == 409 and rejected.json()['detail'] == 'figure_shipping_changed'
    assert db.query(models.Order).count() == 1


@pytest.mark.parametrize('previous,current', [(10, 12.9), (12.9, 0), (0, 12.9)])
def test_shipping_changes_never_reprice_old_carts_and_only_charge_once(client, db, quantity_order, monkeypatch, previous, current):
    config = db.get(models.FigureShipping, 1)
    config.shipping_fee = previous; db.commit()
    old = client.post('/api/orders', json=quantity_order).json()
    config.shipping_fee = current; db.commit()
    fresh = client.post('/api/orders', json={**quantity_order, 'quantity': 2, 'expected_shipping_fee': current}).json()
    assert fresh['id'] != old['id']
    assert (fresh['shipping_fee'], fresh['total_price']) == (current, 60 + current)
    merged = client.post('/api/orders', json={**quantity_order, 'expected_shipping_fee': current}).json()
    assert merged['id'] == fresh['id']
    assert (merged['shipping_fee'], merged['total_price']) == (current, 90 + current)
    original = client.get(f"/api/orders/{old['id']}").json()
    assert (original['shipping_fee'], original['total_price']) == (previous, 30 + previous)
    monkeypatch.setattr('routers.order.s3_client.delete_object', Mock())
    assert client.delete(f"/api/orders/items/{merged['items'][0]['id']}").status_code == 200
    reduced = client.get(f"/api/orders/{fresh['id']}").json()
    assert (reduced['shipping_fee'], reduced['total_price']) == (current, 60 + current)


def test_shipping_migration_preserves_saved_order_fees(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/f2b68c904d15_add_figure_shipping_settings.py'
    spec = spec_from_file_location('figure_shipping_migration', path)
    migration = module_from_spec(spec); spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE orders (id VARCHAR(16), shipping_fee FLOAT, total_price FLOAT)'))
        connection.execute(sa.text("INSERT INTO orders VALUES ('existing', 10, 40)"))
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert connection.execute(sa.text('SELECT shipping_fee, total_price FROM orders')).one() == (10, 40)
        assert Decimal(str(connection.execute(sa.text('SELECT shipping_fee FROM figure_shipping')).scalar_one())) == Decimal('12.90')
        migration.downgrade()
        assert connection.execute(sa.text('SELECT shipping_fee, total_price FROM orders')).one() == (10, 40)
    engine.dispose()
