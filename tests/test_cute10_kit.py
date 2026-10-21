"""The active model changes without changing previously purchased kits."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

import models
from test_order import mock_auth, quantity_order


def migration(filename="a9c64e280fb1_replace_cute_kit_with_cute10.py"):
    path = Path(__file__).resolve().parents[1] / "alembic/versions" / filename
    spec = spec_from_file_location("cute10_kit_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cute10_catalog_migration_preserves_prices_stock_and_existing_order_snapshots(client, db, quantity_order, monkeypatch, mock_auth):
    old = client.post("/api/orders", json=quantity_order)
    assert old.status_code == 200, old.text
    old_item = deepcopy(old.json()["items"][0])
    assert old_item["sticker_snapshot"]["model_name"] == "CUTE-7cm"
    product = db.query(models.ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    old_specs = deepcopy(product.kit_specifications)
    product.price = 43
    product.stock = 279
    db.commit()
    upgrade = migration()
    monkeypatch.setattr(upgrade, "op", SimpleNamespace(get_bind=db.connection))
    upgrade.upgrade()
    db.commit()
    db.expire_all()
    assert (product.price, product.stock) == (43, 279)
    assert product.kit_specifications == upgrade.CUTE_KIT_SPECIFICATIONS
    assert product.kit_specifications["dimensions"] == "Approx. 10 × 6.7 × 4.1 cm"
    materials = {item["name"]: item["quantity"] for item in product.kit_specifications["materials"]}
    assert materials == {"Pre-cut sticker sheet": 1, "White 3D printed body parts": 6, "Long joint": 1, "Short joint": 4}
    saved_old = client.get(f"/api/orders/{old.json()['id']}").json()["items"][0]
    for key in ["kit_specifications_snapshot", "sticker_snapshot", "skin_url", "price"]:
        assert saved_old[key] == old_item[key]
    new = client.post("/api/orders", json={**quantity_order, "quantity": 2, "sticker_snapshot": {"model_name": "Forged"}})
    assert new.status_code == 200, new.text
    items = [item for item in new.json()["items"] if item["id"] != old_item["id"]]
    assert len(items) == 2
    for item in items:
        assert item["price"] == 43
        assert item["kit_specifications_snapshot"] == upgrade.CUTE_KIT_SPECIFICATIONS
        assert item["sticker_snapshot"]["model_name"] == "CUTE-10cm"
        assert item["sticker_snapshot"]["skin_id"] == old_item["sticker_snapshot"]["skin_id"]
    snapshots = {item.id: (deepcopy(item.kit_specifications_snapshot), deepcopy(item.sticker_snapshot)) for item in db.query(models.OrderItem)}
    upgrade.downgrade()
    db.commit()
    db.expire_all()
    assert product.kit_specifications == old_specs
    assert (product.price, product.stock) == (43, 279)
    assert {item.id: (item.kit_specifications_snapshot, item.sticker_snapshot) for item in db.query(models.OrderItem)} == snapshots


@pytest.mark.parametrize("filename", [
    "d7f4c8302a91_add_cute10_spare_sticker.py",
    "e8a42f617c03_add_cute10_sticker_tools.py",
])
def test_material_updates_preserve_existing_snapshots(client, db, quantity_order, monkeypatch, mock_auth, filename):
    cute10 = migration()
    monkeypatch.setattr(cute10, "op", SimpleNamespace(get_bind=db.connection))
    cute10.upgrade()
    if filename.endswith("add_cute10_sticker_tools.py"):
        spare = migration("d7f4c8302a91_add_cute10_spare_sticker.py")
        monkeypatch.setattr(spare, "op", SimpleNamespace(get_bind=db.connection))
        spare.upgrade()
    db.commit()
    product = db.query(models.ModelSalesLimit).filter_by(model_type="Cute DIY Kit").one()
    product.price = 31
    product.stock = 278
    product.kit_specifications = {**product.kit_specifications, "assembly_note": "Custom assembly instructions."}
    db.commit()
    original = deepcopy(product.kit_specifications)
    old = client.post("/api/orders", json={**quantity_order, "checkout_mode": "buy_now"})
    assert old.status_code == 200, old.text

    update = migration(filename)
    monkeypatch.setattr(update, "op", SimpleNamespace(get_bind=db.connection))
    update.upgrade()
    update.upgrade()
    db.commit()
    db.refresh(product)
    expected = deepcopy(original)
    if filename.endswith("add_cute10_spare_sticker.py"):
        expected["materials"][0].update(quantity=2, description="Includes 1 spare sheet.")
    else:
        expected["materials"].extend([
            {"name": "Tweezers", "quantity": 1, "description": "For sticker application."},
            {"name": "Flat-head screwdriver", "quantity": 1, "description": "For sticker application."},
        ])
    assert product.kit_specifications == expected
    assert (product.price, product.stock) == (31, 278)
    public = client.get("/api/orders/model-stock?order_type=print")
    assert public.status_code == 200, public.text
    kit = next(item for item in public.json() if item["model_type"] == "Cute DIY Kit")
    assert kit["kit_specifications"] == expected
    saved = client.get(f"/api/orders/{old.json()['id']}").json()
    assert saved["items"][0]["kit_specifications_snapshot"] == original
    assert saved["total_price"] == old.json()["total_price"] == 41

    new = client.post("/api/orders", json={**quantity_order, "checkout_mode": "buy_now"})
    assert new.status_code == 200, new.text
    assert new.json()["items"][0]["kit_specifications_snapshot"] == expected
    assert new.json()["total_price"] == 41
    update.downgrade()
    db.commit()
    db.refresh(product)
    assert product.kit_specifications == original
    assert client.get(f"/api/orders/{new.json()['id']}").json()["items"][0]["kit_specifications_snapshot"] == expected
