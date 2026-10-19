from pathlib import Path
import importlib.util
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from config import settings
from creator_referrals import price_offer
from test_order import mock_auth, quantity_order
from test_creator_referrals import codes, create

pytestmark = pytest.mark.usefixtures('mock_auth')


@pytest.fixture
def promotion(db):
    value = models.FigureDiscount(id=1, discount_bps=5000, version=1)
    db.add(value); db.commit()
    return value


def test_admin_discount_configuration_auth_validation_and_conflicts(client, db, monkeypatch, promotion):
    endpoint = '/api/figure/models/discount'
    payload = {'discount_bps': 2500, 'expected_version': 1}
    assert client.get(endpoint).status_code == 403
    assert client.patch(endpoint, json=payload).status_code == 403
    monkeypatch.setattr(settings, 'ADMIN_EMAILS', 'test_order@example.com')
    assert client.get(endpoint).json() == {'discount_bps': 5000, 'version': 1}
    for invalid in [-1, 10000, 12.5, True, '5000']:
        assert client.patch(endpoint, json={**payload, 'discount_bps': invalid}).status_code == 422
    saved = client.patch(endpoint, json=payload)
    assert saved.status_code == 200 and saved.json() == {'discount_bps': 2500, 'version': 2}
    assert client.patch(endpoint, json=payload).status_code == 409
    assert client.patch(endpoint, json={'discount_bps': 0, 'expected_version': 2}).json() == {'discount_bps': 0, 'version': 3}


def test_public_catalog_discounts_print_only(client, db, promotion, quantity_order):
    db.add(models.ModelSalesLimit(model_type='pro-test', order_type='subscription', price=100, stock=10)); db.commit()
    result = client.get('/api/orders/model-stock').json()
    kit = next(row for row in result if row['model_type'] == 'Cute DIY Kit')
    assert kit['price'] == 30 and kit['discounted_price'] == 15 and kit['global_discount_bps'] == 5000
    subscription = next(row for row in result if row['model_type'] == 'pro-test')
    assert subscription['price'] == subscription['discounted_price'] == 100 and subscription['global_discount_bps'] == 0


@pytest.mark.parametrize('code,creator_amount,total,commission', [('', 0, 40, 0), ('ALPHA', 3, 37, 4.05), ('BETA', 6, 34, 2.4)])
def test_quote_order_and_commission_stack_sequentially(client, db, codes, promotion, quantity_order, code, creator_amount, total, commission):
    quote = client.post('/api/creators/quote', json={'model_type': 'Cute DIY Kit', 'quantity': 2, 'code': code}).json()
    assert quote['subtotal'] == 60 and quote['global_discount_amount'] == 30
    assert quote['creator_discount_amount'] == creator_amount and quote['total_price'] == total
    assert quote['discount_amount'] == 30 + creator_amount and quote['shipping_fee'] == 10
    order = create(client, quantity_order, quantity=2, creator_code=code, global_discount_version=quote['global_discount_version'],
                   global_discount_bps=0, discount_amount=0, total_price=1)
    assert order['total_price'] == total and order['discount_amount'] == quote['discount_amount']
    assert order['global_discount_amount'] == 30 and order['creator_discount_amount'] == creator_amount
    stored = db.get(models.Order, order['id'])
    assert stored.creator_commission == commission and stored.creator_commission_cents == round(commission * 100)


def test_snapshot_survives_admin_changes_merges_removal_and_paypal(client, db, codes, promotion, quantity_order, monkeypatch):
    monkeypatch.setattr('routers.order.s3_client.delete_object', Mock())
    order = create(client, quantity_order, quantity=2, creator_code='ALPHA', global_discount_version=1)
    merged = create(client, quantity_order, creator_code='ALPHA', global_discount_version=1)
    assert merged['id'] == order['id'] and merged['total_price'] == 50.5
    promotion.discount_bps = 2000; promotion.version = 2; db.commit()
    assert client.post('/api/orders', json={**quantity_order, 'global_discount_version': 1}).status_code == 409
    fresh = create(client, quantity_order, creator_code='ALPHA', global_discount_version=2)
    assert fresh['id'] != order['id'] and fresh['total_price'] == 31.6
    assert client.delete(f"/api/orders/items/{merged['items'][0]['id']}").status_code == 200
    reduced = client.get(f"/api/orders/{order['id']}").json()
    assert reduced['price'] == 60 and reduced['global_discount_amount'] == 30 and reduced['creator_discount_amount'] == 3
    assert reduced['total_price'] == 37
    checkout = Mock(return_value={'id': 'GLOBAL-PAYPAL', 'status': 'CREATED'})
    monkeypatch.setattr('routers.order.create_paypal_order_api', checkout)
    assert client.post(f"/api/orders/{order['id']}/create-paypal-order").status_code == 200
    assert checkout.call_args.args == (37, order['id'])
    assert client.delete(f"/api/orders/items/{reduced['items'][0]['id']}").status_code == 409


def test_global_only_voucher_is_immutable_and_does_not_merge(client, db, promotion, quantity_order, monkeypatch):
    order = create(client, quantity_order, creator_code='')
    checkout = Mock(return_value={'id': 'GLOBAL-ONLY-PAYPAL', 'status': 'CREATED'})
    monkeypatch.setattr('routers.order.create_paypal_order_api', checkout)
    assert client.post(f"/api/orders/{order['id']}/create-paypal-order").status_code == 200
    assert checkout.call_args.args == (25, order['id'])
    assert client.delete(f"/api/orders/items/{order['items'][0]['id']}").status_code == 409
    fresh = create(client, quantity_order, creator_code='')
    assert fresh['id'] != order['id']


def test_cent_rounding_and_zero_discount():
    offer = {'discount_bps': 1000, 'commission_bps': 1500}
    assert price_offer(30.01, 10, offer, 5000) == (16.51, 23.5, 2.03)
    assert price_offer(100, 10, offer, 5000) == (55, 55, 6.75)
    assert price_offer(100, 10, offer, 0) == (10, 100, 13.5)


def test_migration_enables_half_off_only_for_new_orders(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/b8e17d930a42_add_global_figure_discount.py'
    spec = importlib.util.spec_from_file_location('figure_discount_migration', path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE orders (id VARCHAR(16), total_price FLOAT)'))
        connection.execute(sa.text("INSERT INTO orders VALUES ('existing', 40)"))
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert tuple(connection.execute(sa.text('SELECT total_price, global_discount_bps, global_discount_version FROM orders')).one()) == (40, 0, 0)
        assert tuple(connection.execute(sa.text('SELECT discount_bps, version FROM figure_discounts')).one()) == (5000, 1)
        migration.downgrade()
        assert 'global_discount_bps' not in {column['name'] for column in sa.inspect(connection).get_columns('orders')}
    engine.dispose()
