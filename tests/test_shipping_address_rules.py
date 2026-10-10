"""Country-dependent address checks run before any write or PayPal request."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from auth import get_current_user
from main import app
from models import User, ShippingAddress, Order, ModelSalesLimit
from routers import order as orders
from shipping_address_rules import RULES, ShippingAddressError, validate_address, paypal_shipping_address

VALID = {'recipient_name': 'Test Recipient', 'country': 'US', 'state': 'CA', 'city': 'San Jose', 'zip_code': '95131', 'detail_address': '2211 N First Street\nBuilding 17', 'phone': '+1 555 0100', 'is_default': False}


@pytest.fixture
def buyer(db):
    user = User(id='address-buyer', email='address@example.test', username='Buyer')
    db.add(user)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize('field,value,error', [
    ('recipient_name', ' ', 'required'),
    ('country', 'China', 'too_long'),
    ('country', 'ZZ', 'country_invalid'),
    ('country', '', 'required'),
    ('state', ' ', 'required'),
    ('state', 'Not a state', 'state_invalid'),
    ('city', '', 'required'),
    ('zip_code', '', 'required'),
    ('zip_code', 'abc', 'postal_format'),
    ('detail_address', '', 'required'),
    ('detail_address', 'a' * 601, 'street_length'),
    ('phone', ' ', 'required'),
])
def test_invalid_create_is_rejected_without_changing_defaults(client, db, buyer, field, value, error):
    default = ShippingAddress(user_id=buyer.id, **{**VALID, 'is_default': True})
    db.add(default)
    db.commit()
    payload = {**VALID, field: value, 'is_default': True}
    response = client.post('/api/addresses', json=payload)
    assert response.status_code == 422
    assert response.json()['detail']['fields'][field] == error
    assert db.query(ShippingAddress).filter_by(user_id=buyer.id).count() == 1
    db.refresh(default)
    assert default.is_default


@pytest.mark.parametrize('country,state,city,postal', [
    ('CN', '北京市', '北京市', '100000'),
    ('C2', 'Beijing', 'Beijing', '100000'),
    ('CA', 'Ontario', 'Ottawa', 'k1a 0b1'),
    ('GB', '', 'London', 'sw1a 1aa'),
    ('JP', 'Tokyo', 'Shibuya', '150-0002'),
    ('HK', 'Kowloon', '', ''),
    ('MO', '', '', ''),
    ('AE', '', 'Dubai', ''),
    ('SG', '', '', '546080'),
])
def test_valid_regional_addresses_can_be_saved(client, buyer, country, state, city, postal):
    payload = {**VALID, 'country': country, 'state': state, 'city': city, 'zip_code': postal}
    response = client.post('/api/addresses', json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['country'] == ('CN' if country == 'C2' else country)
    assert saved['zip_code'] == postal.upper()
    if country == 'CA':
        assert saved['state'] == 'ON'


def test_country_only_edit_validates_the_merged_address_atomically(client, db, buyer):
    address = ShippingAddress(user_id=buyer.id, **{**VALID, 'country': 'AE', 'state': '', 'city': 'Dubai', 'zip_code': ''})
    db.add(address)
    db.commit()
    response = client.put(f'/api/addresses/{address.id}', json={'country': 'US', 'is_default': True})
    assert response.status_code == 422
    assert response.json()['detail']['fields'] == {'state': 'required', 'zip_code': 'required'}
    db.refresh(address)
    assert address.country == 'AE' and not address.is_default
    response = client.put(f'/api/addresses/{address.id}', json={'country': 'US', 'state': 'California', 'city': 'San Jose', 'zip_code': '95131'})
    assert response.status_code == 200
    assert response.json()['state'] == 'CA'


@pytest.mark.parametrize('field', ['recipient_name', 'country', 'state', 'zip_code', 'phone'])
def test_explicit_null_cannot_bypass_required_fields_on_update(client, db, buyer, field):
    address = ShippingAddress(user_id=buyer.id, **VALID)
    db.add(address)
    db.commit()
    response = client.put(f'/api/addresses/{address.id}', json={field: None})
    assert response.status_code == 422
    db.refresh(address)
    assert getattr(address, field) == VALID[field]


def test_legacy_address_is_readable_but_must_be_corrected_before_reuse(client, db, buyer, monkeypatch):
    address = ShippingAddress(user_id=buyer.id, **{**VALID, 'state': '', 'zip_code': ''})
    order = Order(user_id=buyer.id, order_type='print', status='pending_payment', price=40, total_price=40, address_snapshot={**VALID, 'state': '', 'zip_code': ''})
    product = ModelSalesLimit(model_type='Address Test', order_type='print', stock=10, price=40)
    db.add_all([address, order, product])
    db.commit()
    create = Mock()
    monkeypatch.setattr(orders, 'create_paypal_order_api', create)
    assert client.get('/api/addresses').status_code == 200
    before = deepcopy(order.address_snapshot)
    response = client.post('/api/orders', json={'order_type': 'print', 'model_type': product.model_type, 'address_id': address.id})
    assert response.status_code == 409
    response = client.post(f'/api/orders/{order.id}/create-paypal-order', json={'return_url': 'http://localhost:5173/credits?payment_redirect=1'})
    assert response.status_code == 409
    create.assert_not_called()
    db.refresh(order)
    assert order.address_snapshot == before


def test_paypal_payload_uses_normalized_postal_fields_and_cross_border_china_code():
    address = paypal_shipping_address({**VALID, 'country': 'C2', 'state': 'Beijing', 'city': 'Beijing', 'zip_code': '１０００００'})
    assert address['country_code'] == 'C2' and address['postal_code'] == '100000'
    assert paypal_shipping_address({**VALID, 'state': 'california'})['admin_area_1'] == 'CA'
    assert 'postal_code' not in paypal_shipping_address({**VALID, 'country': 'AE', 'state': '', 'zip_code': ''})
    with pytest.raises(ShippingAddressError):
        paypal_shipping_address({**VALID, 'country': 'HK', 'state': 'Kowloon', 'zip_code': '000000'})


def test_all_documented_postal_examples_and_shared_frontend_snapshot():
    for country, rule in RULES['countries'].items():
        if rule['postal_pattern'] and rule['postal_example']:
            import re
            assert re.fullmatch(rule['postal_pattern'], rule['postal_example'], re.ASCII | re.IGNORECASE), country
    frontend = Path(__file__).resolve().parents[2] / 'entropydrop_frontend/src/constants/shipping-address-rules.json'
    if frontend.exists():
        assert json.loads(frontend.read_text()) == RULES


def test_recipient_limits_and_unicode(client, db, buyer):
    for recipient in ('', '   ', '名' * 301):
        response = client.post('/api/addresses', json={**VALID, 'recipient_name': recipient})
        assert response.status_code == 422
    missing = {key: value for key, value in VALID.items() if key != 'recipient_name'}
    assert client.post('/api/addresses', json=missing).status_code == 422
    assert db.query(ShippingAddress).filter_by(user_id=buyer.id).count() == 0
    for recipient in ('王小明 José O’Neil', '名' * 300):
        response = client.post('/api/addresses', json={**VALID, 'recipient_name': '  ' + recipient if len(recipient) < 300 else recipient})
        assert response.status_code == 200, response.text
        assert response.json()['recipient_name'] == recipient


def test_missing_recipient_is_readable_but_requires_explicit_correction(client, db, buyer):
    address = ShippingAddress(user_id=buyer.id, **{**VALID, 'recipient_name': ''})
    db.add(address)
    db.commit()
    assert client.get('/api/addresses').json()[0]['recipient_name'] == ''
    assert client.put(f'/api/addresses/{address.id}', json={'city': 'San Jose'}).status_code == 422
    response = client.put(f'/api/addresses/{address.id}', json={'recipient_name': 'Correct Recipient'})
    assert response.status_code == 200
    db.refresh(address)
    assert address.recipient_name == 'Correct Recipient'


def test_recipient_migration_preserves_existing_addresses_and_order_snapshots(monkeypatch):
    import importlib.util
    from sqlalchemy import create_engine, text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/e4b7c9a21035_add_shipping_recipient_name.py'
    spec = importlib.util.spec_from_file_location('recipient_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE shipping_addresses (id VARCHAR(16) PRIMARY KEY, city VARCHAR(100))'))
        conn.execute(text("INSERT INTO shipping_addresses (id, city) VALUES ('old', 'Original City')"))
        conn.execute(text('CREATE TABLE orders (id VARCHAR(16) PRIMARY KEY, address_snapshot JSON)'))
        snapshot = json.dumps({'city': 'Original City'})
        conn.execute(text("INSERT INTO orders (id, address_snapshot) VALUES ('old-order', :snapshot)"), {'snapshot': snapshot})
        monkeypatch.setattr(migration, 'op', Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.execute(text('SELECT recipient_name, city FROM shipping_addresses')).one() == ('', 'Original City')
        assert conn.execute(text('SELECT address_snapshot FROM orders')).scalar_one() == snapshot
        column = next(c for c in inspect(conn).get_columns('shipping_addresses') if c['name'] == 'recipient_name')
        assert column['type'].length == 300 and not column['nullable']
