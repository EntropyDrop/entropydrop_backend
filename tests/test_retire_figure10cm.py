"""Retired products disappear from sale without rewriting order history."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from test_figure_catalog import catalog


@pytest.fixture
def migration(db, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/c9f25a7e104d_remove_legacy_figure10cm.py"
    spec = spec_from_file_location("retire_figure10cm_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(db.connection())))
    return module


def rows(db, model):
    return db.execute(sa.select(model.__table__).order_by(model.id)).mappings().all()


@pytest.mark.parametrize("has_snapshot", [False, True])
def test_retirement_removes_sales_entry_and_preserves_history(client, db, catalog, migration, has_snapshot):
    catalog['old'].model_type = 'Figure10cm'
    catalog['kit'].kit_specifications = None
    catalog['order'].status = 'paid'
    catalog['order'].price = catalog['order'].total_price = 60
    catalog['order'].address_snapshot = None
    catalog['item'].model_type = 'Figure10cm'
    catalog['item'].price = 60
    if has_snapshot:
        catalog['item'].kit_specifications_snapshot = {
            'product_name': 'Figure10cm', 'dimensions': '10 cm',
            'materials': [{'name': 'Printed figure', 'quantity': 1}],
            'assembly_note': 'Legacy product',
        }
    db.add(models.FigureDiscount(id=1, discount_bps=5000, version=3))
    db.flush()
    before = {model: rows(db, model) for model in [models.Order, models.OrderItem, models.FigureDiscount]}
    remaining = [row for row in rows(db, models.ModelSalesLimit) if row['model_type'] != 'Figure10cm']

    migration.upgrade()
    # Already absent is safe, and rollback does not silently put it back on sale.
    migration.upgrade()
    migration.downgrade()
    db.commit()
    db.expire_all()
    assert rows(db, models.ModelSalesLimit) == remaining
    for model, original in before.items():
        assert rows(db, model) == original

    for endpoint in ['/api/figure/models', '/api/orders/model-stock?order_type=print']:
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        assert [row['model_type'] for row in response.json()] == ['Cute DIY Kit']
    historical = client.get('/api/orders/catalog-order')
    assert historical.status_code == 200, historical.text
    assert historical.json()['total_price'] == 60
    item = historical.json()['items'][0]
    assert (item['model_type'], item['price']) == ('Figure10cm', 60)
    assert (item['kit_specifications_snapshot'] is not None) == has_snapshot
    unavailable = client.post('/api/orders', json={
        'model_type': 'Figure10cm', 'order_type': 'print', 'shipping_source': 'paypal',
    })
    assert unavailable.status_code == 400
    assert unavailable.json()['detail'] == 'Invalid model type'
    assert db.query(models.Order).count() == 1


def test_retirement_does_not_remove_other_names_or_order_types(db, migration):
    db.add_all([
        models.ModelSalesLimit(id='other-type', model_type='Figure10cm', order_type='subscription', price=10, stock=3),
        models.ModelSalesLimit(id='other-name', model_type='Figure10cm V2', order_type='print', price=70, stock=5),
    ])
    db.flush()
    before = rows(db, models.ModelSalesLimit)
    migration.upgrade()
    assert rows(db, models.ModelSalesLimit) == before
