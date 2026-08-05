import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import instance_monitor


class HeartbeatRedis:
    def __init__(self, values=None):
        self.values = values or {}
        self.zsets = {}

    def set(self, key, value, ex=None):
        self.values[key] = value.encode("utf-8") if isinstance(value, str) else value
        return True

    def get(self, key):
        return self.values.get(key)

    def scan_iter(self, match=None):
        prefix = (match or "").rstrip("*")
        keys = set(self.values) | set(self.zsets)
        return iter(key for key in keys if key.startswith(prefix))

    def pipeline(self, transaction=False):
        return self

    def zadd(self, name, mapping):
        target = self.zsets.setdefault(name, {})
        for member, score in mapping.items():
            encoded = member.encode("utf-8") if isinstance(member, str) else member
            target[encoded] = float(score)
        return len(mapping)

    def zremrangebyscore(self, name, minimum, maximum):
        target = self.zsets.setdefault(name, {})
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float("inf") if maximum == "+inf" else float(maximum)
        removed = [member for member, score in target.items() if low <= score <= high]
        for member in removed:
            del target[member]
        return len(removed)

    def zrangebyscore(self, name, minimum, maximum):
        target = self.zsets.get(name, {})
        low = float("-inf") if minimum == "-inf" else float(minimum)
        high = float("inf") if maximum == "+inf" else float(maximum)
        return [
            member
            for member, score in sorted(target.items(), key=lambda item: item[1])
            if low <= score <= high
        ]

    def expire(self, name, seconds):
        return True

    def execute(self):
        return True


def test_collect_backend_instance_metrics_from_ecs_metadata(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://metadata.local/v4")
    monkeypatch.setenv("GIT_COMMIT", "abcdef123456")

    task_metadata = {
        "Cluster": "production",
        "TaskARN": "arn:aws:ecs:us-east-1:123:task/cluster/task-id-1234",
        "Family": "ed-api",
        "Revision": "42",
        "AvailabilityZone": "us-east-1b",
        "Limits": {"CPU": 1, "Memory": 512},
        "EphemeralStorageMetrics": {"Utilized": 5_120, "Reserved": 20_480},
    }
    container_stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 2_000_000_000},
            "system_cpu_usage": 8_000_000_000,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000_000},
            "system_cpu_usage": 4_000_000_000,
        },
        "memory_stats": {
            "usage": 256 * 1024 * 1024,
            "limit": 512 * 1024 * 1024,
        },
    }

    def read_metadata(url, timeout=1.5):
        return task_metadata if url.endswith("/task") else container_stats

    with patch("instance_monitor._read_json", side_effect=read_metadata):
        result = instance_monitor.collect_backend_instance_metrics()

    assert result["instance_id"] == "task-id-1234"
    assert result["availability_zone"] == "us-east-1b"
    assert result["task_revision"] == "42"
    assert result["git_commit"] == "abcdef123456"
    assert result["cpu"] == {"percent": 100.0, "allocated_vcpus": 1.0}
    assert result["memory"]["percent"] == 50.0
    assert result["disk"]["percent"] == 25.0


def test_publish_and_list_multiple_instances_marks_stale():
    redis = HeartbeatRedis()
    now = datetime.now(timezone.utc)
    healthy = {
        "instance_id": "healthy-id",
        "display_name": "api-healthy",
        "last_heartbeat": now.isoformat(),
        "status": "healthy",
    }
    stale = {
        "instance_id": "stale-id",
        "display_name": "api-stale",
        "last_heartbeat": (now - timedelta(seconds=45)).isoformat(),
        "status": "healthy",
    }
    redis.values = {
        f"{instance_monitor.INSTANCE_KEY_PREFIX}healthy-id": json.dumps(healthy).encode(),
        f"{instance_monitor.INSTANCE_KEY_PREFIX}stale-id": json.dumps(stale).encode(),
        "unrelated": b"ignored",
    }

    result = instance_monitor.list_backend_instance_metrics(redis, stale_after_seconds=30)

    assert result["healthy_count"] == 1
    assert result["total_instances"] == 2
    assert [item["status"] for item in result["instances"]] == ["healthy", "stale"]


def test_publish_uses_instance_specific_key():
    redis = HeartbeatRedis()
    snapshot = {
        "instance_id": "task-abc",
        "display_name": "api-task-abc",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "cpu": {"percent": 18.5},
        "memory": {"percent": 42.0},
        "disk": {"percent": 11.0},
    }

    snapshot["runtime_status"] = "RUNNING"
    with patch("instance_monitor.collect_backend_instance_metrics", return_value=snapshot):
        result = instance_monitor.publish_backend_instance_metrics(
            redis,
            ttl_seconds=120,
            readiness_check=lambda: {"database": "ok", "redis": "ok"},
        )

    assert result["status"] == "healthy"
    assert result["readiness"] == "ready"
    stored = redis.get(f"{instance_monitor.INSTANCE_KEY_PREFIX}task-abc")
    assert json.loads(stored.decode())["instance_id"] == "task-abc"
    history = instance_monitor.list_backend_instance_history(redis, history_hours=12)
    assert history["series"][0]["samples"][0]["cpu_percent"] == 18.5


def test_publish_marks_instance_unhealthy_when_dependency_is_not_ready():
    redis = HeartbeatRedis()
    snapshot = {
        "instance_id": "task-unhealthy",
        "display_name": "api-task-unhealthy",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "runtime_status": "RUNNING",
    }

    with patch("instance_monitor.collect_backend_instance_metrics", return_value=snapshot):
        result = instance_monitor.publish_backend_instance_metrics(
            redis,
            ttl_seconds=120,
            readiness_check=lambda: {"database": "error: TimeoutError", "redis": "ok"},
        )

    assert result["status"] == "unhealthy"
    assert result["readiness"] == "not_ready"


def test_history_keeps_one_sample_per_bucket_and_multiple_instances():
    redis = HeartbeatRedis()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

    def snapshot(instance_id, at, cpu):
        return {
            "instance_id": instance_id,
            "display_name": f"api-{instance_id}",
            "last_heartbeat": at.isoformat(),
            "status": "healthy",
            "cpu": {"percent": cpu},
            "memory": {"percent": 40.0},
            "disk": {"percent": 20.0},
        }

    instance_monitor._record_backend_instance_history(redis, snapshot("one", now, 10.0), 12, 300)
    instance_monitor._record_backend_instance_history(redis, snapshot("one", now + timedelta(seconds=30), 30.0), 12, 300)
    instance_monitor._record_backend_instance_history(redis, snapshot("two", now, 50.0), 12, 300)

    result = instance_monitor.list_backend_instance_history(redis, history_hours=12, bucket_seconds=300)

    assert result["bucket_seconds"] == 300
    assert len(result["series"]) == 2
    first_series = next(item for item in result["series"] if item["instance_id"] == "one")
    assert len(first_series["samples"]) == 1
    assert first_series["samples"][0]["cpu_percent"] == 30.0
