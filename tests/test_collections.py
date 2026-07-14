import pytest
from models import User, Collection, CollectionItem, UserLike, GenerationLog
from auth import get_current_user
import uuid
import models

@pytest.fixture(autouse=True)
def mock_auth(db):
    user = User(
        id="1",
        email="test_col@example.com",
        username="Col User",
        terms_agreed=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    from main import app
    def mock_get_current_user():
        return user
    app.dependency_overrides[get_current_user] = mock_get_current_user
    yield
    app.dependency_overrides.clear()

def test_create_collection(client, db):
    response = client.post("/skin/api/collections", json={
        "name": "My New Collection",
        "is_public": True
    })
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "My New Collection"
    assert data["is_public"] is True

    col = db.query(Collection).filter(Collection.name == "My New Collection").first()
    assert col is not None

def test_get_collections(client, db):
    col_pub = Collection(user_id="1", name="Collection A", is_public=True)
    col_priv = Collection(user_id="1", name="Collection B", is_public=False)
    db.add_all([col_pub, col_priv])
    db.commit()

    # Test default (all)
    response = client.get("/skin/api/collections")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) >= 2

    # Test is_public=true
    response = client.get("/skin/api/collections", params={"is_public": True})
    assert response.status_code == 200
    data = response.json()
    assert all(c["is_public"] for c in data["items"])
    assert any(c["name"] == "Collection A" for c in data["items"])
    assert not any(c["name"] == "Collection B" for c in data["items"])

    # Test is_public=false
    response = client.get("/skin/api/collections", params={"is_public": False})
    assert response.status_code == 200
    data = response.json()
    assert all(not c["is_public"] for c in data["items"])
    assert any(c["name"] == "Collection B" for c in data["items"])
    assert not any(c["name"] == "Collection A" for c in data["items"])

def test_update_collection(client, db):
    col = Collection(user_id="1", name="Old Name", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    response = client.put(f"/skin/api/collections/{col.id}", json={
        "name": "New Name",
        "is_public": False
    })
    assert response.status_code == 200
    
    db.refresh(col)
    assert col.name == "New Name"
    assert col.is_public is True

def test_update_collection_ignores_visibility_changes(client, db):
    col = Collection(user_id="1", name="Private Collection", is_public=False)
    db.add(col)
    db.commit()
    db.refresh(col)

    response = client.put(f"/skin/api/collections/{col.id}", json={
        "name": "Try Public",
        "is_public": True,
    })

    assert response.status_code == 200
    db.refresh(col)
    assert col.is_public is False
    assert col.name == "Try Public"

def test_delete_collection(client, db):
    col = Collection(user_id="1", name="To Delete", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    response = client.delete(f"/skin/api/collections/{col.id}")
    assert response.status_code == 200

    deleted_col = db.query(Collection).filter(Collection.id == col.id).first()
    assert deleted_col is None

def test_get_collection_items_empty(client, db):
    col = Collection(user_id="1", name="Collection Items", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    response = client.get(f"/skin/api/collections/items?collection_id={col.id}&user_id=1")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["items"]) == 0

def test_add_collection_item(client, db):
    col = Collection(user_id="1", name="Add Item Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    payload = {
        "collection_id": col.id,
        "name": "Item 1",
        "type": "image",
        "data": {"url": "http://example.com/1.png"}
    }
    response = client.post("/skin/api/collections/items", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Item 1"

    # Verify in DB
    item = db.query(CollectionItem).filter(CollectionItem.collection_id == col.id).first()
    assert item is not None

def test_delete_collection_item(client, db):
    col = Collection(user_id="1", name="Del Item Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    item = CollectionItem(
        collection_id=col.id,
        type="image",
        data={"url": "http://example.com/2.png"}
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    response = client.delete(f"/skin/api/collections/items/{item.id}")
    assert response.status_code == 200
    
    deleted = db.query(CollectionItem).filter(CollectionItem.id == item.id).first()
    assert deleted is None

def test_move_collection_item(client, db):
    col1 = Collection(user_id="1", name="Collection 1", is_public=True)
    col2 = Collection(user_id="1", name="Collection 2", is_public=True)
    db.add_all([col1, col2])
    db.commit()
    db.refresh(col1)
    db.refresh(col2)

    item = CollectionItem(
        collection_id=col1.id,
        type="image",
        data={"url": "http://example.com/3.png"}
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    payload = {"target_collection_id": col2.id}
    response = client.post(f"/skin/api/collections/items/{item.id}/move", json=payload)
    assert response.status_code == 200
    
    db.refresh(item)
    assert item.collection_id == col2.id

def test_get_log_collections(client, db):
    col = Collection(user_id="1", name="Log Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    log_id = models.generate_base58_id()
    item = CollectionItem(
        collection_id=col.id,
        type="image",
        log_id=log_id,
        data={}
    )
    db.add(item)
    db.commit()

    response = client.get(f"/skin/api/logs/{log_id}/collections")
    assert response.status_code == 200
    data = response.json()
    assert col.id in data

def test_update_log_collections(client, db):
    col1 = Collection(user_id="1", name="Log Collection 1", is_public=True)
    col2 = Collection(user_id="1", name="Log Collection 2", is_public=True)
    db.add_all([col1, col2])
    db.commit()
    db.refresh(col1)
    db.refresh(col2)

    log = GenerationLog(
        id=models.generate_base58_id(),
        user_id="1",
        mode="edit",
        is_public=True
    )
    db.add(log)
    db.commit()

    # Initial addition
    item = CollectionItem(collection_id=col1.id, type="image", log_id=log.id)
    db.add(item)
    db.commit()

    # Update to col2
    payload = [col2.id]
    response = client.post(f"/skin/api/logs/{log.id}/collections", json=payload)
    assert response.status_code == 200
    
    # Verify in DB
    items = db.query(CollectionItem).filter(CollectionItem.log_id == log.id).all()
    assert len(items) == 1
    assert items[0].collection_id == col2.id

def test_get_log_public_collections(client, db):
    col = Collection(user_id="1", name="Public Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    log_id = models.generate_base58_id()
    item = CollectionItem(collection_id=col.id, type="image", log_id=log_id)
    db.add(item)
    db.commit()

    response = client.get(f"/skin/api/logs/{log_id}/public_collections")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1

def test_get_user_public_collections(client, db):
    col = Collection(user_id="1", name="User Public Collection", is_public=True)
    db.add(col)
    db.commit()

    response = client.get("/skin/api/users/1/collections")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1

def test_upload_item_virtual(client, db):
    from unittest.mock import patch
    with patch("s3_utils.s3_client") as mock_s3:
        from PIL import Image
        import io
        img = Image.new('RGB', (64, 64))
        img_io = io.BytesIO()
        img.save(img_io, format='PNG')
        img_bytes = img_io.getvalue()

        files = {"file": ("test.png", img_bytes, "image/png")}
        response = client.post("/skin/api/collections/creations_public/upload", files=files, data={"name": "Upload Test"})
        
        assert response.status_code == 200
        data = response.json()
        assert data["collection_id"] == "creations_public"
        assert data["name"] == "Upload Test"

def test_upload_item_custom_fail(client, db):
    col = Collection(user_id="1", name="Custom Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    files = {"file": ("test.png", b"fake_data", "image/png")}
    response = client.post(f"/skin/api/collections/{col.id}/upload", files=files, data={"name": "Upload Fail Test"})
    assert response.status_code == 400
    assert "Custom collections do not support manual uploads" in response.json()["detail"]

def test_upload_item_invalid_mode(client, db):
    from unittest.mock import patch
    with patch("s3_utils.s3_client") as mock_s3:
        mock_s3.put_object.return_value = {}
        
        files = {"file": ("test.png", b"fake_data", "image/png")}
        # Invalid mode
        response = client.post("/skin/api/collections/creations_public/upload", files=files, data={"mode": "invalid_mode"})
        
        assert response.status_code == 400
        assert "Invalid mode" in response.json()["detail"]

def test_delete_collection_not_found(client, db):
    response = client.delete("/skin/api/collections/invalid_id")
    assert response.status_code == 404
    assert "Collection not found" in response.json()["detail"]

def test_add_private_item_to_public_collection(client, db):
    # Public Collection
    col = Collection(user_id="1", name="Public Collection", is_public=True)
    db.add(col)
    db.commit()
    db.refresh(col)

    # Private Log
    log = GenerationLog(
        id=models.generate_base58_id(),
        user_id="1",
        mode="edit",
        is_public=False
    )
    db.add(log)
    db.commit()

    payload = {
        "collection_id": col.id,
        "name": "Item Private",
        "type": "image",
        "log_id": log.id,
        "data": {}
    }
    response = client.post("/skin/api/collections/items", json=payload)
    assert response.status_code == 400
    assert "Private images cannot be added to public collections" in response.json()["detail"]

def test_upload_private_model_as_public_fail(client, db):
    from unittest.mock import patch
    # Private Parent Log
    parent_log = GenerationLog(
        id=models.generate_base58_id(),
        user_id="1",
        mode="edit",
        is_public=False
    )
    db.add(parent_log)
    db.commit()

    with patch("s3_utils.s3_client") as mock_s3:
        mock_s3.put_object.return_value = {}
        
        files = {"file": ("test.png", b"fake_data", "image/png")}
        response = client.post("/skin/api/collections/creations_public/upload", files=files, data={"parent": parent_log.id, "mode": "human_upload"})
        
        assert response.status_code == 400
        assert "Private models cannot be saved as public" in response.json()["detail"]

def test_add_other_user_private_item_fail(client, db):
    # User B (Another User)
    user_b = User(
        id="2",
        email="user_b@example.com",
        username="User B",
        terms_agreed=True
    )
    db.add(user_b)
    db.commit()
    
    # User B's Private Log
    log = GenerationLog(
        id=models.generate_base58_id(),
        user_id="2",  # Belongs to User B
        mode="edit",
        is_public=False
    )
    db.add(log)
    
    # User A's Private Collection (User A is current_user "1" from mock_auth)
    col = Collection(user_id="1", name="User A Private Collection", is_public=False)
    db.add(col)
    db.commit()
    db.refresh(col)

    # User A tries to add User B's private log to User A's private collection
    payload = {
        "collection_id": col.id,
        "name": "Steal Item",
        "type": "image",
        "log_id": log.id,
        "data": {}
    }
    response = client.post("/skin/api/collections/items", json=payload)
    assert response.status_code == 403  # Should be forbidden
    
    # Also test update_log_collections
    payload2 = [col.id]
    response2 = client.post(f"/skin/api/logs/{log.id}/collections", json=payload2)
    assert response2.status_code == 403

def test_upload_item_too_large(client, db):
    files = {"file": ("test.png", b"A" * (513 * 1024), "image/png")}
    response = client.post("/skin/api/collections/creations_public/upload", files=files)
    assert response.status_code == 413
    assert "Request entity too large" in response.json()["detail"]

def test_upload_item_invalid_dimensions(client, db):
    from PIL import Image
    import io
    img = Image.new('RGB', (32, 32))
    img_io = io.BytesIO()
    img.save(img_io, format='PNG')
    img_bytes = img_io.getvalue()

    files = {"file": ("test.png", img_bytes, "image/png")}
    response = client.post("/skin/api/collections/creations_public/upload", files=files)
    assert response.status_code == 400
    assert "Invalid dimensions" in response.json()["detail"]
