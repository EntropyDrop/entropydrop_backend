"""PayPal capture responses may omit purchase-unit custom_id."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

import payment_utils
from auth import get_current_user
from main import app
from models import User, Order, OrderItem, ModelSalesLimit
from routers import order as orders


@pytest.fixture
def payment(db, monkeypatch):
    user = User(id='capture-user', email='capture@example.test', username='Capture')
    db.add(user)
    product = ModelSalesLimit(model_type='Capture Kit', order_type='print', stock=10, price=40)
    order = Order(id='capture-order', user_id=user.id, order_type='print', status='pending_payment', price=40, shipping_fee=0, total_price=40, paypal_order_id='PAYPAL-CAPTURE')
    db.add_all([product, order])
    db.flush()
    db.add(OrderItem(order_id=order.id, model_type=product.model_type, price=40))
    db.commit()
    approved = {'id': order.paypal_order_id, 'status': 'APPROVED', 'purchase_units': [{
        'custom_id': order.id, 'amount': {'currency_code': 'USD', 'value': '40.00'},
    }]}
    completed = deepcopy(approved)
    completed['status'] = 'COMPLETED'
    completed['purchase_units'][0]['payments'] = {'captures': [{
        'id': 'CAPTURE-ID', 'status': 'COMPLETED', 'custom_id': order.id,
        'amount': {'currency_code': 'USD', 'value': '40.00'},
    }]}
    capture = Mock()
    get = Mock(side_effect=[approved, completed])
    monkeypatch.setattr(orders, 'capture_paypal_order_api', capture)
    monkeypatch.setattr(orders, 'get_paypal_order_api', get)
    app.dependency_overrides[get_current_user] = lambda: user
    yield order, product, approved, completed, capture, get
    app.dependency_overrides.pop(get_current_user, None)


def capture_response(payment, shape):
    order, _, _, completed, _, _ = payment
    response = {'id': order.paypal_order_id, 'status': 'COMPLETED'}
    if shape == 'capture_only':
        # PayPal can put custom_id on the capture, without repeating it on the unit.
        response['purchase_units'] = [{
            'reference_id': 'default',
            'payments': deepcopy(completed['purchase_units'][0]['payments']),
        }]
    return response


@pytest.mark.parametrize('shape', ['minimal', 'capture_only'])
def test_capture_without_unit_binding_fetches_order_before_fulfillment(client, db, payment, shape):
    order, product, _, _, capture, get = payment
    capture.return_value = capture_response(payment, shape)
    response = client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id})
    assert response.status_code == 200, response.text
    db.refresh(order)
    db.refresh(product)
    assert order.status == 'paid'
    assert order.figure_review_status == 'pending'
    assert order.goods_status == 'awaiting_review'
    assert order.paypal_capture_id == 'CAPTURE-ID'
    assert product.stock == 9
    assert get.call_count == 2
    capture.assert_called_once_with(order.paypal_order_id)
    # Retrying after the ambiguous capture must not capture or consume stock twice.
    assert client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id}).status_code == 200
    db.refresh(product)
    assert product.stock == 9
    assert get.call_count == 2
    capture.assert_called_once()


@pytest.mark.parametrize('field,value,error', [
    ('custom_id', 'another-local-order', 'PayPal order binding mismatch'),
    ('custom_id', None, 'PayPal order binding mismatch'),
    ('value', '1.00', 'PayPal amount mismatch'),
    ('currency_code', 'EUR', 'PayPal currency mismatch'),
    ('id', 'OTHER-PAYPAL-ORDER', 'Payment voucher mismatch'),
    ('status', 'APPROVED', 'PayPal payment not completed'),
])
def test_incomplete_capture_rechecks_full_order_before_marking_paid(client, db, payment, field, value, error):
    order, _, approved, completed, capture, get = payment
    capture.return_value = capture_response(payment, 'capture_only')
    invalid = deepcopy(completed)
    if field in ('id', 'status'):
        invalid[field] = value
        if field == 'status':
            invalid['purchase_units'][0]['payments']['captures'][0]['status'] = 'PENDING'
    elif field == 'custom_id':
        invalid['purchase_units'][0][field] = value
    else:
        invalid['purchase_units'][0]['payments']['captures'][0]['amount'][field] = value
    get.side_effect = [approved, invalid]
    response = client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id})
    assert response.status_code == 400
    assert response.json()['detail'] == error
    db.refresh(order)
    assert order.status == 'pending_payment'
    assert order.figure_review_status is None
    assert order.capture_started
    assert order.inventory_reserved


@pytest.mark.parametrize('field,value,error', [
    ('id', 'OTHER-PAYPAL-ORDER', 'Payment voucher mismatch'),
    ('custom_id', 'another-local-order', 'PayPal order binding mismatch'),
])
def test_explicit_capture_binding_mismatch_is_not_hidden_by_refetch(client, db, payment, field, value, error):
    order, _, _, completed, capture, get = payment
    invalid = deepcopy(completed)
    if field == 'id':
        invalid = capture_response(payment, 'minimal')
        invalid[field] = value
    else:
        invalid['purchase_units'][0][field] = value
    capture.return_value = invalid
    response = client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id})
    assert response.status_code == 400
    assert response.json()['detail'] == error
    assert get.call_count == 1
    db.refresh(order)
    assert order.status == 'pending_payment'


def test_order_detail_timeout_after_capture_recovers_without_recapturing(client, db, payment):
    order, product, approved, completed, capture, get = payment
    capture.return_value = capture_response(payment, 'minimal')
    get.side_effect = [approved, TimeoutError('order lookup unavailable'), completed]
    response = client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id})
    assert response.status_code == 503
    db.refresh(order)
    assert order.status == 'pending_payment' and order.capture_started
    response = client.post(f'/api/orders/{order.id}/pay', json={'paypal_order_id': order.paypal_order_id})
    assert response.status_code == 200
    db.refresh(order)
    db.refresh(product)
    assert order.status == 'paid'
    assert order.paypal_capture_id == 'CAPTURE-ID'
    assert product.stock == 9
    capture.assert_called_once()


def test_capture_requests_complete_resource_representation(monkeypatch):
    monkeypatch.setattr(payment_utils, 'get_paypal_access_token', lambda: 'mock-token')
    response = Mock()
    response.json.return_value = {'id': 'PAYPAL-CAPTURE', 'status': 'COMPLETED'}
    post = Mock(return_value=response)
    monkeypatch.setattr(payment_utils.requests, 'post', post)
    payment_utils.capture_paypal_order_api('PAYPAL-CAPTURE')
    assert post.call_args.kwargs['headers']['Prefer'] == 'return=representation'
    response.raise_for_status.assert_called_once()
