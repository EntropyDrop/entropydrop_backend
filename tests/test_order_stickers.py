from copy import deepcopy
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from unittest.mock import Mock
import pytest
from fastapi import HTTPException
import sqlalchemy as sa
import auth
import models
import skin_withdrawal
from main import app
from routers.order import clone_skin_for_order as original_clone
from test_order import mock_auth, quantity_order

pytestmark = pytest.mark.usefixtures("mock_auth")


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/d2f81a604bc9_add_order_sticker_snapshot.py"
    spec = spec_from_file_location("order_sticker_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_private_order_copy_and_sticker_survive_source_withdrawal_and_profile_removal(client, db, quantity_order, withdrawal_storage, monkeypatch):
    from config import settings
    source = db.query(models.GenerationLog).filter_by(id=quantity_order["log_id"]).one()
    maker = models.User(id="original-maker", username="Pixel 制作者", email="maker@example.test")
    db.add(maker)
    source.user_id = maker.id
    source.name = "Original skin title"
    db.commit()
    files = {(settings.AWS_BUCKET_NAME, source.result): b"original skin pixels"}
    def copy_object(**kwargs):
        origin = kwargs["CopySource"]
        files[(kwargs["Bucket"], kwargs["Key"])] = files[(origin["Bucket"], origin["Key"])]
    def delete_object(**kwargs):
        files.pop((kwargs["Bucket"], kwargs["Key"]), None)
    storage, _ = withdrawal_storage
    storage.delete_object.side_effect = delete_object
    monkeypatch.setattr("routers.order.clone_skin_for_order", original_clone)
    monkeypatch.setattr("routers.order.s3_client.copy_object", copy_object)
    sign = Mock(side_effect=lambda key, **kwargs: f"https://order-files.example.test/{key}")
    monkeypatch.setattr("s3_utils.generate_presigned_url_get", sign)
    response = client.post("/api/orders", json={**quantity_order, "quantity": 2, "sticker_language": "zh-hans", "sticker_snapshot": {"publisher_name": "Forged"}})
    assert response.status_code == 200
    data = response.json()
    saved_address = deepcopy(data["address"])
    saved = deepcopy(data["items"][0]["sticker_snapshot"])
    assert saved["skin_id"] == source.id and saved["skin_name"] == "Original skin title"
    assert saved["publisher_id"] == maker.id and saved["publisher_name"] == "Pixel 制作者"
    assert saved["brand"] == "EntropyDrop" and saved["model_name"] == "CUTE-7cm"
    assert saved["labels"] == {"publisher": "发布者", "user_id": "用户 ID", "source": "皮肤"}
    assert saved["source_url"] == f"https://entropydrop.com/skin/?id={source.id}"
    assert saved["origin"] == "order" and saved["missing_fields"] == []
    assert all(item["sticker_snapshot"] == saved for item in data["items"])
    assert all(files[(settings.AWS_PRIVATE_BUCKET_NAME, f"orders/{data['id']}/{item['id']}.png")] == b"original skin pixels" for item in data["items"])
    skin_withdrawal.begin(db, source)
    skin_withdrawal.resume(db, source)
    assert source.result is None and source.is_deleted
    assert all(not key.startswith("orders/") for key in [call.kwargs["Key"] for call in storage.delete_object.call_args_list])
    db.delete(source)
    maker.username = "Changed name"
    db.commit()
    db.delete(maker)
    db.commit()
    address_path = f"/api/addresses/{quantity_order['address_id']}"
    assert client.put(address_path, json={"recipient_name": "Changed Recipient", "detail_address": "New street"}).status_code == 200
    assert client.delete(address_path).status_code == 200
    # Even after all source records disappear, checkout uses the order's saved
    # recipient/address and production uses its independent files and text.
    create_payment = Mock(return_value={"id": "PAYPAL-SAVED-SOURCE", "status": "CREATED"})
    monkeypatch.setattr("routers.order.create_paypal_order_api", create_payment)
    payment = client.post(f"/api/orders/{data['id']}/create-paypal-order")
    assert payment.status_code == 200, payment.text
    create_payment.assert_called_once_with(80, data["id"],
        shipping_name=saved_address["recipient_name"], shipping_address={"country_code": "US", "admin_area_1": "NY", "admin_area_2": "NYC", "address_line_1": "Test address", "postal_code": "10001"})
    order = db.query(models.Order).filter_by(id=data["id"]).one()
    order.status = "paid"
    order.figure_review_status = "approved"
    db.commit()
    app.dependency_overrides[auth.get_current_admin] = lambda: db.query(models.User).filter_by(id="1").one()
    for item in data["items"]:
        path = f"/api/figure/orders/{data['id']}/items/{item['id']}/production-source"
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json()["sticker_snapshot"] == saved
        assert response.json()["skin_url"].endswith(f"orders/{data['id']}/{item['id']}.png")
        assert files[(settings.AWS_PRIVATE_BUCKET_NAME, f"orders/{data['id']}/{item['id']}.png")] == b"original skin pixels"
        # Refreshing a bookmarked production link signs its copy again.
        count = sign.call_count
        assert client.get(path).status_code == 200
        assert sign.call_count == count + 1
    readback = client.get(f"/api/orders/{data['id']}").json()
    assert readback["address"] == saved_address
    assert all(item["sticker_snapshot"] == saved for item in readback["items"])
    assert all(item["kit_specifications_snapshot"] == data["items"][0]["kit_specifications_snapshot"] for item in readback["items"])
    admin = client.get("/api/figure/orders", params={"stage": "production", "order_id": data["id"]})
    assert admin.status_code == 200
    assert admin.json()["items"][0]["address"] == saved_address
    assert all(item["source_snapshot"]["publisher_name"] == "Pixel 制作者" for item in admin.json()["items"][0]["items"])
    assert all(item.source_snapshot["publisher_name"] == "Pixel 制作者" for item in db.query(models.OrderItem).filter_by(order_id=data["id"]))


def test_production_source_requires_admin_approval_item_ownership_and_complete_snapshot(client, db, quantity_order, monkeypatch):
    data = client.post("/api/orders", json=quantity_order).json()
    item_id = data["items"][0]["id"]
    path = f"/api/figure/orders/{data['id']}/items/{item_id}/production-source"
    monkeypatch.setattr(auth.settings, "ADMIN_EMAILS", "admin-only@example.test")
    assert client.get(path).status_code == 403
    app.dependency_overrides[auth.get_current_admin] = lambda: db.query(models.User).filter_by(id="1").one()
    assert client.get(path).status_code == 409
    order = db.query(models.Order).filter_by(id=data["id"]).one()
    order.status = "paid"; order.figure_review_status = "approved"; db.commit()
    assert client.get(path).status_code == 200
    assert client.get(f"/api/figure/orders/{data['id']}/items/not-an-item/production-source").status_code == 404
    item = db.query(models.OrderItem).filter_by(id=item_id).one()
    item.sticker_snapshot = {**item.sticker_snapshot, "missing_fields": ["publisher_name"]}; db.commit()
    assert client.get(path).status_code == 409
    item.sticker_snapshot = None; db.commit()
    assert client.get(path).status_code == 409


def test_order_cannot_be_created_without_a_source_image(client, db, quantity_order):
    source = db.query(models.GenerationLog).filter_by(id=quantity_order["log_id"]).one()
    source.result = None; db.commit()
    assert client.post("/api/orders", json=quantity_order).status_code == 409
    assert db.query(models.OrderItem).count() == 0


def test_copy_failure_does_not_leave_an_order_without_its_skin(client, db, quantity_order, monkeypatch):
    monkeypatch.setattr("routers.order.clone_skin_for_order", original_clone)
    monkeypatch.setattr("routers.order.s3_client.copy_object", Mock(side_effect=RuntimeError("storage unavailable")))
    assert client.post("/api/orders", json=quantity_order).status_code == 500
    assert db.query(models.OrderItem).count() == 0


def test_sticker_migration_recovers_legacy_data_without_fabricating_missing_names(monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    items = sa.Table("order_items", metadata, sa.Column("id", sa.String, primary_key=True), sa.Column("model_type", sa.String), sa.Column("refer_log_id", sa.String), sa.Column("source_snapshot", sa.JSON))
    logs = sa.Table("generation_logs", metadata, sa.Column("id", sa.String), sa.Column("name", sa.String), sa.Column("prompt", sa.String), sa.Column("user_id", sa.String), sa.Column("is_deleted", sa.Boolean))
    users = sa.Table("users", metadata, sa.Column("id", sa.String), sa.Column("username", sa.String))
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(users.insert(), {"id": "maker", "username": "Current maker"})
        connection.execute(logs.insert(), {"id": "deleted", "name": "Deleted", "prompt": None, "user_id": "maker", "is_deleted": True})
        connection.execute(items.insert(), [
            {"id": "saved", "model_type": "Cute DIY Kit", "refer_log_id": "deleted", "source_snapshot": {"skin_id": "deleted", "name": "Saved skin", "publisher_id": "maker", "publisher_name": "Saved maker"}},
            {"id": "recover", "model_type": "Cute DIY Kit", "refer_log_id": "deleted", "source_snapshot": {"skin_id": "deleted", "name": "Saved skin", "publisher_id": "maker"}},
            {"id": "missing", "model_type": "Cute DIY Kit", "refer_log_id": "gone", "source_snapshot": None},
            {"id": "lost-prompt", "model_type": "Cute DIY Kit", "refer_log_id": "deleted", "source_snapshot": {"skin_id": "deleted", "name": None, "publisher_id": "maker"}},
            {"id": "other", "model_type": "Old model", "refer_log_id": None, "source_snapshot": None},
        ])
        migration = load_migration()
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        upgraded = sa.Table("order_items", sa.MetaData(), autoload_with=connection)
        values = {row.id: row.sticker_snapshot for row in connection.execute(sa.select(upgraded))}
        assert values["saved"]["publisher_name"] == "Saved maker"
        assert values["saved"]["skin_name"] == "Saved skin"
        assert values["recover"]["publisher_name"] == "Current maker"
        assert values["recover"]["origin"] == "legacy_backfill"
        assert values["missing"]["skin_name"] == "" and "skin_name" in values["missing"]["missing_fields"]
        assert "publisher_id" in values["missing"]["missing_fields"]
        assert "skin_name" in values["lost-prompt"]["missing_fields"]
        assert values["other"] is None
        migration.downgrade()
        assert "sticker_snapshot" not in {column["name"] for column in sa.inspect(connection).get_columns("order_items")}
    engine.dispose()


def test_skin_copy_rejects_a_missing_source_with_a_clear_conflict():
    with pytest.raises(HTTPException) as error:
        original_clone(None, "order", "item")
    assert error.value.status_code == 409
