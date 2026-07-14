import time

import routers.generate as generate


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)


def test_discovery_cache_roundtrips_through_redis(monkeypatch):
    redis_conn = FakeRedis()
    monkeypatch.setattr(generate, "redis_conn", redis_conn)
    monkeypatch.setattr(generate, "DISCOVERY_CACHE_KEY", "test:discovery")

    items = [{"id": "1", "creator": {"id": "u1", "username": "Alice"}}]
    generate.write_discovery_cache_to_redis(items)

    generate.discovery_cache_items = []
    generate.discovery_cache_last_updated = 0.0

    assert generate.read_discovery_cache_from_redis() == items
    assert generate.discovery_cache_items == items
    assert generate.discovery_cache_last_updated > 0


def test_get_discovery_cache_uses_fresh_local_cache(monkeypatch):
    items = [{"id": "fresh"}]
    generate.discovery_cache_items = items
    generate.discovery_cache_last_updated = time.time()

    def fail_redis_read():
        raise AssertionError("Redis should not be read")

    def fail_db_update():
        raise AssertionError("DB should not be queried")

    monkeypatch.setattr(generate, "read_discovery_cache_from_redis", fail_redis_read)
    monkeypatch.setattr(generate, "update_discovery_cache", fail_db_update)

    assert generate.get_discovery_cache_items() == items
