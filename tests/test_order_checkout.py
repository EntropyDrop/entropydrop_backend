"""Direct popup checkout uses mocked PayPal calls and the isolated test DB."""
from copy import deepcopy
from unittest.mock import Mock
import pytest
from auth import get_current_user
from main import app
from models import User, Order, OrderItem, ModelSalesLimit, ShippingAddress
import payment_utils
from routers import order as orders

RETURN_URL = 'http://localhost:5173/credits?payment_redirect=1'
ADDRESS = {'recipient_name': 'Snapshot Recipient', 'id': 'saved-address', 'user_id': 'checkout-user', 'country': 'US', 'state': 'CA', 'city': 'San Jose', 'detail_address': '2211 N First Street\nBuilding 17', 'zip_code': '95131', 'phone': '+1 555 0100', 'is_default': False}
PAYPAL_ADDRESS = {'country_code': 'US', 'admin_area_1': 'CA', 'admin_area_2': 'San Jose', 'address_line_1': '2211 N First Street', 'address_line_2': 'Building 17', 'postal_code': '95131'}


@pytest.fixture
def checkout(db, monkeypatch):
    user = User(id='checkout-user', email='checkout@example.test', username='Checkout')
    db.add(user)
    product = ModelSalesLimit(model_type='Checkout Kit', order_type='print', stock=10, price=40)
    order = Order(id='checkout-order', user_id=user.id, order_type='print', status='pending_payment', price=80, shipping_fee=0, total_price=80, address_snapshot=deepcopy(ADDRESS))
    db.add_all([product, order])
    db.flush()
    db.add_all([OrderItem(order_id=order.id, model_type=product.model_type, price=40) for _ in range(2)])
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    payload = {'id': 'PAYPAL-CHECKOUT', 'status': 'CREATED', 'links': [{'rel': 'approve', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=PAYPAL-CHECKOUT'}]}
    payload['purchase_units'] = [{'reference_id': payment_utils.PAYPAL_WEBSITE_SHIPPING_REFERENCE, 'shipping': {'address': deepcopy(PAYPAL_ADDRESS)}}]
    create = Mock(return_value=payload)
    get = Mock(return_value=payload)
    monkeypatch.setattr(orders, 'create_paypal_order_api', create)
    monkeypatch.setattr(orders, 'get_paypal_order_api', get)
    monkeypatch.setattr(orders.settings, 'PAYPAL_API_BASE', 'https://api-m.sandbox.paypal.com')
    yield order, product, create, get
    app.dependency_overrides.pop(get_current_user, None)


def test_popup_checkout_returns_approval_url_and_server_amount(client, db, checkout):
    order, product, create, _ = checkout
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL, 'amount': 1})
    assert response.status_code == 200
    assert response.json()['approval_url'] == create.return_value['links'][0]['href']
    assert response.json()['id'] == 'PAYPAL-CHECKOUT'
    create.assert_called_once_with(80, order.id, return_url=RETURN_URL, cancel_url=RETURN_URL, shipping_address=PAYPAL_ADDRESS, shipping_name=ADDRESS['recipient_name'])
    db.refresh(order)
    db.refresh(product)
    assert order.paypal_order_id == 'PAYPAL-CHECKOUT'
    assert product.stock == 8 and order.inventory_reserved
    assert order.status == 'pending_payment'


def test_reopening_checkout_reuses_voucher_and_hold(client, db, checkout):
    order, product, create, get = checkout
    url = f'/api/orders/{order.id}/create-paypal-order'
    assert client.post(url, json={'return_url': RETURN_URL}).status_code == 200
    response = client.post(url, json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert response.json()['id'] == 'PAYPAL-CHECKOUT'
    create.assert_called_once()
    get.assert_called_once_with('PAYPAL-CHECKOUT')
    db.refresh(product)
    assert product.stock == 8


def test_old_sdk_clients_remain_compatible_without_request_body(client, checkout):
    order, _, create, get = checkout
    url = f'/api/orders/{order.id}/create-paypal-order'
    for _ in range(2):
        response = client.post(url)
        assert response.status_code == 200
        assert response.json() == {'id': 'PAYPAL-CHECKOUT'}
    create.assert_called_once_with(80, order.id, shipping_address=PAYPAL_ADDRESS, shipping_name=ADDRESS['recipient_name'])
    get.assert_called_once_with('PAYPAL-CHECKOUT')


@pytest.mark.parametrize('host, expected', [('https://api-m.sandbox.paypal.com', 'www.sandbox.paypal.com'), ('https://api-m.paypal.com', 'www.paypal.com')])
def test_old_voucher_fallback_preserves_paypal_environment(client, checkout, monkeypatch, host, expected):
    order, _, create, _ = checkout
    create.return_value = {'id': 'PAYPAL-CHECKOUT', 'links': []}
    monkeypatch.setattr(orders.settings, 'PAYPAL_API_BASE', host)
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.json()['approval_url'] == f'https://{expected}/checkoutnow?token=PAYPAL-CHECKOUT'


def test_existing_approved_voucher_returns_status_for_confirmation(client, db, checkout):
    order, _, create, get = checkout
    order.paypal_order_id = 'PAYPAL-CHECKOUT'
    db.commit()
    get.return_value = {'id': 'PAYPAL-CHECKOUT', 'status': 'APPROVED'}
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert response.json()['status'] == 'APPROVED'
    create.assert_not_called()


def test_initialization_failure_rolls_back_hold(client, db, checkout):
    order, product, create, _ = checkout
    create.side_effect = RuntimeError('PayPal unavailable')
    # SQLite's legacy transaction mode otherwise commits a standalone savepoint.
    # Match PostgreSQL's enclosing transaction when exercising rollback.
    db.connection().exec_driver_sql('BEGIN')
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 500
    db.refresh(order)
    db.refresh(product)
    assert product.stock == 10
    assert not order.inventory_reserved
    assert order.paypal_order_id is None


def test_checkout_rejects_paid_or_foreign_orders(client, db, checkout):
    order, _, create, _ = checkout
    order.status = 'paid'
    db.commit()
    assert client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL}).status_code == 400
    assert client.post('/api/orders/not-owned/create-paypal-order', json={'return_url': RETURN_URL}).status_code == 404
    create.assert_not_called()


def test_checkout_rejects_non_http_return_url(client, checkout):
    order, _, create, _ = checkout
    assert client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': 'javascript:alert(1)'}).status_code == 422
    create.assert_not_called()


def test_uncertain_capture_resumes_the_existing_voucher(client, db, checkout):
    order, product, create, get = checkout
    orders.reserve_inventory(db, order)
    order.capture_started = True
    order.paypal_order_id = 'PAYPAL-CHECKOUT'
    db.commit()
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert response.json() == {'id': 'PAYPAL-CHECKOUT', 'status': 'CAPTURE_PENDING'}
    create.assert_not_called()
    get.assert_not_called()
    db.refresh(product)
    assert product.stock == 8
    assert client.post(f'/api/orders/{order.id}/create-paypal-order').status_code == 409


@pytest.mark.parametrize('status', ['CREATED', 'PAYER_ACTION_REQUIRED'])
@pytest.mark.parametrize('popup', [True, False])
def test_old_unapproved_checkout_is_replaced_with_locked_shipping(client, db, checkout, status, popup):
    order, product, create, get = checkout
    orders.reserve_inventory(db, order)
    order.paypal_order_id = 'OLD-PAYPAL'
    db.commit()
    get.return_value = {'id': 'OLD-PAYPAL', 'status': status, 'purchase_units': [{'reference_id': 'default'}]}
    options = {'json': {'return_url': RETURN_URL}} if popup else {}
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', **options)
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'PAYPAL-CHECKOUT'
    assert create.call_args.kwargs['shipping_address'] == PAYPAL_ADDRESS
    assert create.call_args.kwargs['shipping_name'] == ADDRESS['recipient_name']
    db.refresh(order)
    db.refresh(product)
    assert order.paypal_order_id == 'PAYPAL-CHECKOUT'
    assert order.inventory_reserved and product.stock == 8


@pytest.mark.parametrize('status', ['APPROVED', 'COMPLETED'])
def test_existing_payment_is_not_replaced_when_shipping_was_not_locked(client, db, checkout, status):
    order, _, create, get = checkout
    order.paypal_order_id = 'OLD-PAYPAL'
    db.commit()
    get.return_value = {'id': 'OLD-PAYPAL', 'status': status}
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert response.json()['id'] == 'OLD-PAYPAL'
    assert response.json()['status'] == status
    create.assert_not_called()


def test_checkout_uses_order_snapshot_and_ignores_client_or_address_book_changes(client, db, checkout):
    order, _, create, _ = checkout
    saved = ShippingAddress(**{**ADDRESS, 'recipient_name': 'Different Recipient', 'detail_address': 'A different address'})
    db.add(saved)
    order.address_id = saved.id
    db.commit()
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={
        'return_url': RETURN_URL, 'shipping_address': {'country_code': 'GB'}, 'shipping_name': 'Client replacement',
    })
    assert response.status_code == 200
    assert create.call_args.kwargs['shipping_address'] == PAYPAL_ADDRESS
    assert create.call_args.kwargs['shipping_name'] == ADDRESS['recipient_name']
    db.refresh(order)
    assert order.address_snapshot == ADDRESS


def test_legacy_order_snapshots_its_saved_address_before_checkout(client, db, checkout):
    order, _, create, _ = checkout
    saved = ShippingAddress(**ADDRESS)
    db.add(saved)
    order.address_id = saved.id
    order.address_snapshot = None
    db.commit()
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert create.call_args.kwargs['shipping_address'] == PAYPAL_ADDRESS
    assert create.call_args.kwargs['shipping_name'] == ADDRESS['recipient_name']
    db.refresh(order)
    assert order.address_snapshot['detail_address'] == ADDRESS['detail_address']


@pytest.mark.parametrize('address', [None, {**ADDRESS, 'country': ''}, {**ADDRESS, 'city': ' '}, {**ADDRESS, 'detail_address': 'x' * 601}])
def test_incomplete_or_oversized_shipping_never_opens_an_unlocked_checkout(client, db, checkout, address):
    order, product, create, _ = checkout
    order.address_snapshot = address
    db.commit()
    db.connection().exec_driver_sql('BEGIN')
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 409
    create.assert_not_called()
    db.refresh(order)
    db.refresh(product)
    assert order.paypal_order_id is None
    assert not order.inventory_reserved and product.stock == 10


def test_optional_address_fields_are_omitted_and_long_street_is_preserved():
    street = 'A' * 300 + 'B' * 300
    order = Order(address_snapshot={**ADDRESS, 'country': ' ae ', 'state': '', 'zip_code': '', 'detail_address': street})
    address = orders._paypal_shipping_address(order)
    assert address['country_code'] == 'AE'
    assert address['address_line_1'] + address['address_line_2'] == street
    assert 'admin_area_1' not in address and 'postal_code' not in address


@pytest.mark.parametrize('popup', [True, False])
def test_physical_paypal_payload_locks_website_shipping(monkeypatch, popup):
    monkeypatch.setattr(payment_utils, 'get_paypal_access_token', lambda: 'mock-token')
    response = Mock()
    response.json.return_value = {'id': 'PAYPAL-CHECKOUT'}
    post = Mock(return_value=response)
    monkeypatch.setattr(payment_utils.requests, 'post', post)
    urls = {'return_url': RETURN_URL, 'cancel_url': RETURN_URL} if popup else {}
    payment_utils.create_paypal_order_api(80, 'checkout-order', shipping_address=PAYPAL_ADDRESS, shipping_name=ADDRESS['recipient_name'], **urls)
    payload = post.call_args.kwargs['json']
    unit = payload['purchase_units'][0]
    assert unit['shipping']['address'] == PAYPAL_ADDRESS
    assert unit['shipping']['name'] == {'full_name': ADDRESS['recipient_name']}
    assert unit['reference_id'] == payment_utils.PAYPAL_WEBSITE_SHIPPING_REFERENCE
    assert unit['custom_id'] == 'checkout-order'
    assert unit['amount'] == {'currency_code': 'USD', 'value': '80.00'}
    context = payload['payment_source']['paypal']['experience_context']
    assert context == {**urls, 'shipping_preference': 'SET_PROVIDED_ADDRESS'}
    response.raise_for_status.assert_called_once()


def test_credit_checkout_keeps_existing_payload_without_physical_shipping(monkeypatch):
    monkeypatch.setattr(payment_utils, 'get_paypal_access_token', lambda: 'mock-token')
    post = Mock()
    monkeypatch.setattr(payment_utils.requests, 'post', post)
    payment_utils.create_paypal_order_api(5, 'credit:buyer:50:purchase', return_url=RETURN_URL, cancel_url=RETURN_URL)
    payload = post.call_args.kwargs['json']
    assert 'shipping' not in payload['purchase_units'][0]
    assert 'payment_source' not in payload
    assert payload['application_context'] == {'return_url': RETURN_URL, 'cancel_url': RETURN_URL}


@pytest.mark.parametrize('recipient', [None, '', ' ', '名' * 301])
def test_checkout_requires_recipient_without_guessing_from_account_or_address_book(client, db, checkout, recipient):
    order, product, create, _ = checkout
    snapshot = deepcopy(ADDRESS)
    if recipient is None:
        snapshot.pop('recipient_name')
    else:
        snapshot['recipient_name'] = recipient
    order.address_snapshot = snapshot
    db.commit()
    db.connection().exec_driver_sql('BEGIN')
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 409
    create.assert_not_called()
    db.refresh(order)
    db.refresh(product)
    assert order.address_snapshot == snapshot
    assert product.stock == 10 and not order.inventory_reserved


def test_old_unapproved_v1_voucher_is_replaced_to_include_recipient(client, db, checkout):
    order, _, create, get = checkout
    order.paypal_order_id = 'OLD-PAYPAL'
    db.commit()
    get.return_value = {'id': 'OLD-PAYPAL', 'status': 'CREATED', 'purchase_units': [{'reference_id': 'website-shipping-v1'}]}
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert create.call_args.kwargs['shipping_name'] == ADDRESS['recipient_name']


@pytest.mark.parametrize('status', ['APPROVED', 'COMPLETED'])
def test_approved_legacy_payment_without_recipient_keeps_its_voucher(client, db, checkout, status):
    order, _, create, get = checkout
    order.paypal_order_id = 'OLD-PAYPAL'
    order.address_snapshot = {key: value for key, value in ADDRESS.items() if key != 'recipient_name'}
    db.commit()
    get.return_value = {'id': 'OLD-PAYPAL', 'status': status}
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': RETURN_URL})
    assert response.status_code == 200
    assert response.json()['id'] == 'OLD-PAYPAL'
    create.assert_not_called()


def test_payment_payload_refuses_an_empty_recipient_before_contacting_paypal(monkeypatch):
    token = Mock()
    monkeypatch.setattr(payment_utils, 'get_paypal_access_token', token)
    with pytest.raises(ValueError, match='recipient'):
        payment_utils.create_paypal_order_api(40, 'order', shipping_address=PAYPAL_ADDRESS)
    token.assert_not_called()
