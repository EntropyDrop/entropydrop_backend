import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock Redis globally for testing to avoid polluting real Redis
import redis
from unittest.mock import MagicMock

class FakeRedis(MagicMock):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._store = {}
    
    def get(self, name):
        return self._store.get(name)
        
    def set(self, name, value, ex=None, px=None, nx=False, xx=False, keepttl=False):
        # Redis stores keys/values as bytes or string. Let's store as bytes or convert to match Redis behavior.
        self._store[name] = str(value).encode('utf-8') if not isinstance(value, bytes) else value
        return True
        
    def delete(self, *names):
        for name in names:
            self._store.pop(name, None)
        return len(names)

    def incr(self, name, amount=1):
        val = self._store.get(name, b"0")
        try:
            val_int = int(val)
        except ValueError:
            val_int = 0
        new_val = val_int + amount
        self._store[name] = str(new_val).encode('utf-8')
        return new_val

    def expire(self, name, time):
        return True

_fake_redis_instance = FakeRedis()
redis.Redis.from_url = classmethod(lambda cls, *args, **kwargs: _fake_redis_instance)

from main import app
from database import Base, get_db
import models # Ensure all models are loaded into Base.metadata

from sqlalchemy.pool import StaticPool

# Use SQLite in-memory database for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="function")
def db():
    # Create tables before each test
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        # Drop tables after each test
        Base.metadata.drop_all(bind=engine)

@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass
    
    # Override dependency
    app.dependency_overrides[get_db] = override_get_db
    
    # Disable rate limiting for testing
    if hasattr(app.state, "limiter"):
        app.state.limiter.enabled = False

    # Create client
    c = TestClient(app)
    yield c
    
    # Re-enable rate limiting after test if needed
    if hasattr(app.state, "limiter"):
        app.state.limiter.enabled = True

    # Clear overrides
    app.dependency_overrides.clear()
