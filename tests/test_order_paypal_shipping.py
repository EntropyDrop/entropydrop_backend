"""PayPal address selection is verified and snapshotted before any capture."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
import importlib.util

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
import payment_utils
from routers import order as orders
from test_order import mock_auth, quantity_order

pytestmark = pytest.mark.usefixtures('mock_auth')
SHIPPING = {
    'name': {'full_name': 'PayPal Recipient'},
    'address': {'country_code': 'US', 'admin_area_1': 'California', 'admin_area_2': 'San Jose',
                'address_line_1': '2211 N First Street', 'address_line_2': 'Building 17', 'postal_code': '95131'},
}


def paypal_request(request):
    return {**{key: value for key, value in request.items() if key != 'address_id'}, 'shipping_source': 'paypal'}


@pytest.fixture
def wallet(client, db, quantity_order, monkeypatch):
    response = client.post('/api/orders', json=paypal_request(quantity_order))
    assert response.status_code == 200, response.text
    order = db.query(models.Order).filter_by(id=response.json()['id']).one()
    created = {'id': 'WALLET-PAYPAL', 'status': 'CREATED', 'purchase_units': [{'reference_id': payment_utils.PAYPAL_WALLET_SHIPPING_REFERENCE}]}
    create = Mock(return_value=created)
    get = Mock(return_value=created)
    monkeypatch.setattr(orders, 'create_paypal_order_api', create)
    monkeypatch.setattr(orders, 'get_paypal_order_api', get)
    assert client.post(f'/api/orders/{order.id}/create-paypal-order').status_code == 200
    approved = {'id': 'WALLET-PAYPAL', 'status': 'APPROVED', 'purchase_units': [{
        'custom_id': order.id, 'reference_id': payment_utils.PAYPAL_WALLET_SHIPPING_REFERENCE,
        'amount': {'currency_code': 'USD', 'value': '40.00'}, 'shipping': deepcopy(SHIPPING),
    }]}
    captured = deepcopy(approved)
    captured['status'] = 'COMPLETED'
    captured['purchase_units'][0]['payments'] = {'captures': [{'id': 'WALLET-CAPTURE', 'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '40.00'}}]}
    capture = Mock(return_value=captured)
    monkeypatch.setattr(orders, 'capture_paypal_order_api', capture)
    get.return_value = approved
    return order, approved, captured, create, get, capture


def pay(client, order, **extra):
    return client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': 'WALLET-PAYPAL', **extra})


def test_paypal_orders_need_no_saved_address_and_merge_only_with_paypal_orders(client, db, quantity_order):
    website = client.post('/api/orders', json=quantity_order).json()
    request = paypal_request(quantity_order)
    response = client.post('/api/orders', json={**request, 'quantity': 2, 'address_snapshot': {'country': 'forged'}})
    assert response.status_code == 200, response.text
    wallet = response.json()
    assert wallet['shipping_source'] == 'paypal'
    assert wallet['address_id'] is None and wallet['address'] is None
    assert wallet['id'] != website['id'] and wallet['total_price'] == 70
    assert website['shipping_source'] == 'address_book'
    assert client.delete(f"/api/addresses/{quantity_order['address_id']}").status_code == 200
    merged = client.post('/api/orders', json=request).json()
    assert merged['id'] == wallet['id'] and merged['total_price'] == 100
    assert len(merged['items']) == 3 and merged['address'] is None
    assert db.query(models.ShippingAddress).count() == 0


def test_shipping_source_rejects_ambiguous_invalid_and_missing_address_book_input(client, db, quantity_order):
    assert client.post('/api/orders', json={**quantity_order, 'shipping_source': 'paypal'}).status_code == 400
    assert client.post('/api/orders', json={**quantity_order, 'shipping_source': 'untrusted'}).status_code == 422
    request = {key: value for key, value in quantity_order.items() if key != 'address_id'}
    assert client.post('/api/orders', json={**request, 'shipping_source': 'address_book'}).status_code == 400
    assert db.query(models.Order).count() == 0


def test_paypal_checkout_reuses_matching_voucher_without_a_website_snapshot(client, db, wallet):
    order, approved, _, create, get, _ = wallet
    create.assert_called_once_with(40, order.id, use_paypal_shipping=True)
    get.return_value = {**approved, 'status': 'CREATED'}
    for body in [None, {'return_url': 'https://example.test/return'}]:
        result = client.post(f'/api/orders/{order.id}/create-paypal-order', json=body)
        assert result.status_code == 200 and result.json()['id'] == 'WALLET-PAYPAL'
    create.assert_called_once()
    db.refresh(order)
    assert order.address_snapshot is None and order.inventory_reserved


def test_payment_saves_verified_shipping_before_capture_and_is_idempotent(client, db, wallet):
    order, _, captured, _, _, capture = wallet
    def capture_with_snapshot(_):
        db.refresh(order)
        assert order.capture_started and order.address_snapshot['recipient_name'] == 'PayPal Recipient'
        assert order.address_snapshot['phone'] == ''
        assert order.inventory_reserved
        return captured
    capture.side_effect = capture_with_snapshot
    result = pay(client, order, address_snapshot={'recipient_name': 'Forged'}, shipping_source='address_book')
    assert result.status_code == 200, result.text
    data = result.json()
    assert data['status'] == 'paid' and data['goods_status'] == 'awaiting_review'
    assert data['address']['state'] == 'CA'
    assert data['address']['detail_address'] == '2211 N First Street\nBuilding 17'
    assert data['address']['recipient_name'] == 'PayPal Recipient'
    assert data['address_id'] is None
    db.refresh(order)
    assert order.paypal_capture_id == 'WALLET-CAPTURE' and order.inventory_consumed
    original = deepcopy(order.address_snapshot)
    assert pay(client, order).status_code == 200
    capture.assert_called_once()
    assert db.query(models.ShippingAddress).count() == 1, 'PayPal shipping must not create an address-book entry'
    assert client.get(f'/api/orders/{order.id}').json()['address'] == original


@pytest.mark.parametrize('change', ['missing_shipping', 'missing_name', 'missing_street', 'missing_primary_street', 'malformed_street', 'missing_city', 'invalid_country', 'invalid_postal', 'malformed_name', 'multiple_units'])
def test_invalid_shipping_never_captures_and_allows_a_fresh_checkout(client, db, wallet, change):
    order, approved, _, create, _, capture = wallet
    unit = approved['purchase_units'][0]
    if change == 'missing_shipping': unit.pop('shipping')
    elif change == 'missing_name': unit['shipping'].pop('name')
    elif change == 'missing_street': unit['shipping']['address'].pop('address_line_1'); unit['shipping']['address'].pop('address_line_2')
    elif change == 'missing_primary_street': unit['shipping']['address'].pop('address_line_1')
    elif change == 'malformed_street': unit['shipping']['address']['address_line_1'] = ['bad']
    elif change == 'missing_city': unit['shipping']['address']['admin_area_2'] = ''
    elif change == 'invalid_country': unit['shipping']['address']['country_code'] = 'ZZ'
    elif change == 'invalid_postal': unit['shipping']['address']['postal_code'] = 'abc'
    elif change == 'malformed_name': unit['shipping']['name']['full_name'] = {'bad': 'data'}
    elif change == 'multiple_units': approved['purchase_units'].append(deepcopy(unit))
    result = pay(client, order)
    assert result.status_code == 409, result.text
    assert result.json()['detail']['code'] == 'invalid_paypal_shipping_address'
    capture.assert_not_called()
    db.refresh(order)
    assert order.status == 'pending_payment' and order.address_snapshot is None
    assert not order.capture_started and order.inventory_reserved and order.paypal_order_id == 'WALLET-PAYPAL'
    # Background reconciliation must not erase the voucher before the browser
    # can receive its actionable address error.
    orders._repair_unhandled_orders(db)
    assert pay(client, order).json()['detail']['code'] == 'invalid_paypal_shipping_address'
    capture.assert_not_called()
    create.return_value = {'id': 'CORRECTED-PAYPAL', 'status': 'CREATED'}
    retried = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': 'https://example.test/return'})
    assert retried.status_code == 200 and retried.json()['id'] == 'CORRECTED-PAYPAL'
    assert create.call_args.kwargs['use_paypal_shipping'] is True


@pytest.mark.parametrize('field,value', [('custom_id', 'another-order'), ('amount', {'currency_code': 'USD', 'value': '1.00'})])
def test_unbound_or_incorrect_amount_never_supplies_shipping_or_captures(client, db, wallet, field, value):
    order, approved, _, _, _, capture = wallet
    approved['purchase_units'][0][field] = value
    assert pay(client, order).status_code == 400
    capture.assert_not_called()
    db.refresh(order)
    assert order.address_snapshot is None and not order.capture_started


def test_capture_timeout_keeps_durable_snapshot_and_reconciles_without_recharging(client, db, wallet):
    order, _, captured, _, get, capture = wallet
    capture.side_effect = RuntimeError('capture response lost')
    assert pay(client, order).status_code == 503
    db.refresh(order)
    assert order.capture_started and order.inventory_reserved
    snapshot = deepcopy(order.address_snapshot)
    captured['purchase_units'][0].pop('shipping')
    get.return_value = captured
    orders._repair_unhandled_orders(db)
    db.refresh(order)
    assert order.status == 'paid' and order.address_snapshot == snapshot
    capture.assert_called_once()


def test_completed_paypal_order_recovers_shipping_without_another_capture(client, db, wallet):
    order, _, captured, _, get, capture = wallet
    get.return_value = captured
    result = pay(client, order)
    assert result.status_code == 200 and result.json()['address']['recipient_name'] == 'PayPal Recipient'
    capture.assert_not_called()


def test_invalid_already_captured_shipping_is_held_for_review_without_losing_payment_reference(client, db, wallet):
    order, _, captured, create, get, capture = wallet
    captured['purchase_units'][0].pop('shipping')
    get.return_value = captured
    result = pay(client, order)
    assert result.status_code == 409
    assert isinstance(result.json()['detail'], str)
    db.refresh(order)
    assert order.paypal_order_id == 'WALLET-PAYPAL'
    assert order.capture_started and order.inventory_reserved_until is None
    orders.expire_inventory_holds(db)
    db.refresh(order)
    assert order.paypal_order_id == 'WALLET-PAYPAL' and order.inventory_reserved
    capture.assert_not_called()
    result = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': 'https://example.test/return'})
    assert result.json() == {'id': 'WALLET-PAYPAL', 'status': 'CAPTURE_PENDING'}
    create.assert_called_once()


@pytest.mark.parametrize('with_urls', [True, False])
def test_wallet_paypal_payload_explicitly_collects_shipping_without_merchant_address(monkeypatch, with_urls):
    monkeypatch.setattr(payment_utils, 'get_paypal_access_token', lambda: 'test-token')
    post = Mock()
    monkeypatch.setattr(payment_utils.requests, 'post', post)
    urls = {'return_url': 'https://example.test/return', 'cancel_url': 'https://example.test/return'} if with_urls else {}
    payment_utils.create_paypal_order_api(40, 'order-a', use_paypal_shipping=True, **urls)
    payload = post.call_args.kwargs['json']
    assert payload['payment_source']['paypal']['experience_context'] == {**urls, 'shipping_preference': 'GET_FROM_FILE'}
    unit = payload['purchase_units'][0]
    assert unit['reference_id'] == payment_utils.PAYPAL_WALLET_SHIPPING_REFERENCE
    assert unit['custom_id'] == 'order-a' and 'shipping' not in unit


def test_shipping_source_migration_preserves_old_order_addresses(monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/b7d8e9a10264_add_order_shipping_source.py'
    spec = importlib.util.spec_from_file_location('shipping_source_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE orders (id VARCHAR(16), address_id VARCHAR(16), address_snapshot JSON)'))
        connection.execute(sa.text("INSERT INTO orders VALUES ('old-order', 'old-address', '{\"recipient_name\":\"Original\"}')"))
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        row = connection.execute(sa.text('SELECT address_id, address_snapshot, shipping_source FROM orders')).one()
        assert tuple(row) == ('old-address', '{"recipient_name":"Original"}', 'address_book')
        migration.downgrade()
        assert 'shipping_source' not in {column['name'] for column in sa.inspect(connection).get_columns('orders')}
    engine.dispose()
