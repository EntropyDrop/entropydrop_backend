import pytest

import auth
import models
from main import app
from order_inventory import reserve_inventory


@pytest.fixture
def catalog(db):
    admin = models.User(id="catalog-admin", email="catalog-admin@example.test")
    customer = models.User(id="catalog-owner", email="catalog-owner@example.test")
    kit = models.ModelSalesLimit(id="catalog-kit", model_type="Cute DIY Kit", order_type="print", stock=300, price=40,
                                kit_specifications={"product_name": "CUTE-10cm"})
    old = models.ModelSalesLimit(id="catalog-old", model_type="PLA+sticker", order_type="print", stock=10, price=60)
    sub = models.ModelSalesLimit(id="catalog-sub", model_type="pro_1m", order_type="subscription", stock=1000, price=10)
    order = models.Order(id="catalog-order", user_id=customer.id, order_type="print", status="pending_payment",
                         price=40, total_price=40, address_snapshot={"recipient_name": "Customer"})
    item = models.OrderItem(id="catalog-item", order_id=order.id, model_type=kit.model_type, price=40)
    db.add_all([admin, customer, kit, old, sub, order, item]); db.commit()
    app.dependency_overrides[auth.get_current_admin] = lambda: admin
    app.dependency_overrides[auth.get_current_user] = lambda: customer
    return dict(admin=admin, customer=customer, kit=kit, old=old, sub=sub, order=order, item=item)


def update(client, **values):
    return client.patch('/api/figure/models/catalog-kit', json=dict(stock=125, price=45.25, expected_stock=300, expected_price=40) | values)


def test_catalog_is_admin_only(client, db, catalog, monkeypatch):
    app.dependency_overrides.pop(auth.get_current_admin)
    monkeypatch.setattr(auth.settings, 'ADMIN_EMAILS', 'catalog-admin@example.test')
    assert client.get('/api/figure/models').status_code == 403
    assert update(client).status_code == 403
    db.refresh(catalog['kit']); assert catalog['kit'].stock == 300
    app.dependency_overrides.pop(auth.get_current_user)
    assert client.get('/api/figure/models').status_code == 401
    assert update(client).status_code == 401


def test_lists_all_print_models_without_subscriptions(client, db, catalog):
    result = client.get('/api/figure/models')
    assert result.status_code == 200
    assert [(p['model_type'], p['stock'], p['price']) for p in result.json()] == [('Cute DIY Kit', 300, 40), ('PLA+sticker', 10, 60)]
    assert result.json()[0]['product_name'] == 'CUTE-10cm'
    assert client.get('/skin/api/figure/models').json() == result.json()


def test_saves_stock_price_and_preserves_existing_orders_and_other_models(client, db, catalog):
    result = update(client, stock=0)
    assert result.status_code == 200, result.text
    assert result.json()['stock'] == 0 and result.json()['price'] == 45.25
    db.refresh(catalog['kit']); db.refresh(catalog['order']); db.refresh(catalog['item'])
    assert catalog['kit'].stock == 0 and catalog['kit'].price == 45.25
    assert catalog['kit'].kit_specifications == {'product_name': 'CUTE-10cm'}
    assert catalog['order'].total_price == 40 and catalog['item'].price == 40
    assert catalog['old'].price == 60 and catalog['sub'].price == 10
    # The public quote reads the same saved catalog; no cache or separate pricing table.
    catalog['kit'].kit_specifications = None; db.commit()
    public = client.get('/api/orders/model-stock?order_type=print').json()
    quote = next(p for p in public if p['model_type'] == 'Cute DIY Kit')
    assert quote['stock'] == 0 and quote['available'] is False and quote['price'] == 45.25
    assert update(client, expected_stock=0, expected_price=45.25).status_code == 200


@pytest.mark.parametrize('values', [
    {'stock': -1}, {'stock': 1.5}, {'stock': True}, {'stock': '12'}, {'stock': 2147483648},
    {'price': 0}, {'price': -1}, {'price': 1.234}, {'price': 'NaN'}, {'price': 'Infinity'},
    {'price': 1000000}, {'price': True}, {'expected_stock': True}, {'expected_price': 'NaN'},
    {'order_type': 'subscription'}, {'model_type': 'other'},
])
def test_rejects_invalid_settings_without_mutation(client, db, catalog, values):
    assert update(client, **values).status_code == 422
    db.refresh(catalog['kit'])
    assert catalog['kit'].stock == 300 and catalog['kit'].price == 40


@pytest.mark.parametrize('model_id', ['missing', 'catalog-sub'])
def test_cannot_create_or_change_subscription_models(client, db, catalog, model_id):
    assert client.patch('/api/figure/models/' + model_id, json=dict(stock=0, price=20, expected_stock=1000, expected_price=10)).status_code == 404
    db.refresh(catalog['sub']); assert catalog['sub'].price == 10


def test_checkout_holds_cannot_be_overwritten_by_stale_admin_form(client, db, catalog):
    assert client.get('/api/figure/models').json()[0]['stock'] == 300
    reserve_inventory(db, catalog['order']); db.commit()
    assert update(client).status_code == 409
    db.refresh(catalog['kit']); db.refresh(catalog['order'])
    assert catalog['kit'].stock == 299 and catalog['kit'].price == 40
    assert catalog['order'].inventory_reserved is True
    assert update(client, stock=100, expected_stock=299).status_code == 200
    # Editing available stock must not clear any existing payment hold.
    db.refresh(catalog['order']); assert catalog['order'].inventory_reserved is True


def test_concurrent_price_change_cannot_be_overwritten(client, db, catalog):
    assert update(client, stock=300, price=50).status_code == 200
    assert update(client).status_code == 409
    db.refresh(catalog['kit'])
    assert catalog['kit'].stock == 300 and catalog['kit'].price == 50


def test_empty_catalog(client, db, catalog):
    db.query(models.ModelSalesLimit).filter_by(order_type='print').delete(); db.commit()
    assert client.get('/api/figure/models').json() == []
