import pytest
from models import User, ShippingAddress
from auth import get_current_user

@pytest.fixture(autouse=True)
def mock_auth(db):
    user = User(
        id="1",
        email="test_addr@example.com",
        username="Addr User"
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

def test_get_addresses_empty(client):
    response = client.get("/skin/api/addresses")
    assert response.status_code == 200
    assert response.json() == []

def test_create_address(client, db):
    payload = {
        "country": "China",
        "phone": "13800000000",
        "zip_code": "100000",
        "state": "Beijing",
        "city": "Beijing",
        "detail_address": "Changan Street No.1",
        "is_default": True
    }
    response = client.post("/skin/api/addresses", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["country"] == "China"
    assert data["phone"] == "13800000000"
    assert data["is_default"] is True

    # Verify in DB
    addr = db.query(ShippingAddress).filter(ShippingAddress.user_id == "1").first()
    assert addr is not None
    assert addr.city == "Beijing"

def test_create_address_limit(client, db):
    # Add 10 addresses first
    for i in range(10):
        addr = ShippingAddress(
            user_id="1",
            country="China",
            phone=f"1380000000{i}",
            zip_code="100000",
            state="Beijing",
            city="Beijing",
            detail_address=f"Street {i}"
        )
        db.add(addr)
    db.commit()

    # Try creating the 11th
    payload = {
        "country": "China",
        "phone": "13800000000",
        "zip_code": "100000",
        "state": "Beijing",
        "city": "Beijing",
        "detail_address": "Street 11"
    }
    response = client.post("/skin/api/addresses", json=payload)
    assert response.status_code == 400
    assert "Maximum of 10 shipping addresses can be stored" in response.json()["detail"]

def test_update_address(client, db):
    addr = ShippingAddress(
        user_id="1",
        country="China",
        phone="13800000000",
        zip_code="100000",
        state="Beijing",
        city="Beijing",
        detail_address="Old Street"
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)

    payload = {
        "detail_address": "New Street",
        "is_default": True
    }
    response = client.put(f"/skin/api/addresses/{addr.id}", json=payload)
    assert response.status_code == 200
    
    db.refresh(addr)
    assert addr.detail_address == "New Street"
    assert addr.is_default is True

def test_delete_address(client, db):
    addr = ShippingAddress(
        user_id="1",
        country="China",
        phone="13800000000",
        zip_code="100000",
        state="Beijing",
        city="Beijing",
        detail_address="To Delete"
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)

    response = client.delete(f"/skin/api/addresses/{addr.id}")
    assert response.status_code == 200
    
    deleted = db.query(ShippingAddress).filter(ShippingAddress.id == addr.id).first()
    assert deleted is None

def test_update_address_not_found(client):
    payload = {"detail_address": "New Street"}
    response = client.put("/skin/api/addresses/invalid_id", json=payload)
    assert response.status_code == 404

def test_delete_address_not_found(client):
    response = client.delete("/skin/api/addresses/invalid_id")
    assert response.status_code == 404
