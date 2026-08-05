"""Per-instance API resource metrics published through Redis heartbeats."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import urlopen


logger = logging.getLogger(__name__)

INSTANCE_KEY_PREFIX = "monitor:backend-instance:"
HISTORY_KEY_PREFIX = "monitor:backend-instance-history:"
MIB = 1024 ** 2
PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()

_cpu_sample_lock = threading.Lock()
_previous_cpu_sample: tuple[float, float] | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percent(used: float | None, limit: float | None) -> float | None:
    if used is None or limit is None or limit <= 0:
        return None
    return round(max(0.0, min(100.0, used / limit * 100)), 1)


def _read_json(url: str, timeout: float = 1.5) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _ecs_cpu_percent(stats: dict[str, Any], allocated_vcpus: float | None) -> float | None:
    cpu_stats = stats.get("cpu_stats") or {}
    previous = stats.get("precpu_stats") or {}
    current_usage = _number((cpu_stats.get("cpu_usage") or {}).get("total_usage"))
    previous_usage = _number((previous.get("cpu_usage") or {}).get("total_usage"))
    current_system = _number(cpu_stats.get("system_cpu_usage"))
    previous_system = _number(previous.get("system_cpu_usage"))

    if None in (current_usage, previous_usage, current_system, previous_system):
        return None

    cpu_delta = current_usage - previous_usage
    system_delta = current_system - previous_system
    if cpu_delta < 0 or system_delta <= 0:
        return None

    online_cpus = _number(cpu_stats.get("online_cpus"))
    if not online_cpus:
        online_cpus = float(len((cpu_stats.get("cpu_usage") or {}).get("percpu_usage") or []))
    if not online_cpus:
        online_cpus = 1.0

    # Docker reports 100% per fully used core. Normalize that value against
    # the vCPU allocation so a task consuming all of its allowance reads 100%.
    core_percent = cpu_delta / system_delta * online_cpus * 100
    capacity = allocated_vcpus if allocated_vcpus and allocated_vcpus > 0 else online_cpus
    return round(max(0.0, min(100.0, core_percent / capacity)), 1)


def _ecs_memory(stats: dict[str, Any]) -> tuple[int | None, int | None]:
    memory = stats.get("memory_stats") or {}
    used = _number(memory.get("usage"))
    limit = _number(memory.get("limit"))
    return (
        int(used) if used is not None else None,
        int(limit) if limit is not None and limit > 0 else None,
    )


def _read_first_number(paths: tuple[str, ...]) -> float | None:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
            if raw and raw != "max":
                return float(raw)
        except (OSError, ValueError):
            continue
    return None


def _fallback_memory() -> tuple[int | None, int | None]:
    used = _read_first_number((
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ))
    limit = _read_first_number((
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ))

    if used is not None and limit is not None and limit < 1 << 60:
        return int(used), int(limit)

    # Local-development fallback. ru_maxrss is KiB on Linux and bytes on macOS.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_bytes = int(peak if os.uname().sysname == "Darwin" else peak * 1024)
    return peak_bytes, None


def _cgroup_cpu_usage_seconds() -> float | None:
    try:
        with open("/sys/fs/cgroup/cpu.stat", "r", encoding="utf-8") as handle:
            values = dict(line.split() for line in handle if len(line.split()) == 2)
        if "usage_usec" in values:
            return float(values["usage_usec"]) / 1_000_000
    except (OSError, ValueError):
        pass

    usage_ns = _read_first_number(("/sys/fs/cgroup/cpuacct/cpuacct.usage",))
    if usage_ns is not None:
        return usage_ns / 1_000_000_000
    return None


def _fallback_cpu_percent(allocated_vcpus: float | None) -> float | None:
    global _previous_cpu_sample

    usage = _cgroup_cpu_usage_seconds()
    if usage is None:
        process = resource.getrusage(resource.RUSAGE_SELF)
        usage = process.ru_utime + process.ru_stime

    now = time.monotonic()
    with _cpu_sample_lock:
        previous = _previous_cpu_sample
        _previous_cpu_sample = (now, usage)

    if previous is None:
        return None

    elapsed = now - previous[0]
    usage_delta = usage - previous[1]
    if elapsed <= 0 or usage_delta < 0:
        return None

    capacity = allocated_vcpus if allocated_vcpus and allocated_vcpus > 0 else float(os.cpu_count() or 1)
    return round(max(0.0, min(100.0, usage_delta / elapsed / capacity * 100)), 1)


def collect_backend_instance_metrics() -> dict[str, Any]:
    metadata_url = os.getenv("ECS_CONTAINER_METADATA_URI_V4", "").rstrip("/")
    task_metadata: dict[str, Any] = {}
    container_stats: dict[str, Any] = {}

    if metadata_url:
        try:
            task_metadata = _read_json(f"{metadata_url}/task")
            container_stats = _read_json(f"{metadata_url}/stats")
        except Exception as exc:
            logger.warning("Could not read ECS task metadata: %s", exc.__class__.__name__)

    task_arn = str(task_metadata.get("TaskARN") or "")
    hostname = socket.gethostname()
    instance_id = task_arn.rsplit("/", 1)[-1] if task_arn else hostname
    limits = task_metadata.get("Limits") or {}
    allocated_vcpus = _number(limits.get("CPU"))

    cpu_percent = _ecs_cpu_percent(container_stats, allocated_vcpus)
    if cpu_percent is None:
        cpu_percent = _fallback_cpu_percent(allocated_vcpus)

    memory_used, memory_limit = _ecs_memory(container_stats)
    if memory_used is None:
        memory_used, fallback_limit = _fallback_memory()
        memory_limit = memory_limit or fallback_limit
    task_memory_mib = _number(limits.get("Memory"))
    if memory_limit is None and task_memory_mib and task_memory_mib > 0:
        memory_limit = int(task_memory_mib * 1024 ** 2)

    storage = task_metadata.get("EphemeralStorageMetrics") or {}
    storage_used_mib = _number(storage.get("Utilized"))
    storage_limit_mib = _number(storage.get("Reserved"))
    if storage_used_mib is not None and storage_limit_mib is not None:
        disk_used = int(storage_used_mib * MIB)
        disk_limit = int(storage_limit_mib * MIB)
    else:
        disk = shutil.disk_usage("/")
        disk_used = disk.used
        disk_limit = disk.total

    now = datetime.now(timezone.utc).isoformat()
    revision = task_metadata.get("Revision")
    short_id = instance_id[:12]
    container_started_at = sorted(
        str(container.get("StartedAt"))
        for container in (task_metadata.get("Containers") or [])
        if container.get("StartedAt")
    )

    return {
        "instance_id": instance_id,
        "display_name": f"api-{short_id}",
        "hostname": hostname,
        "task_arn": task_arn or None,
        "cluster": task_metadata.get("Cluster"),
        "availability_zone": task_metadata.get("AvailabilityZone"),
        "task_family": task_metadata.get("Family"),
        "task_revision": str(revision) if revision is not None else None,
        "runtime_status": task_metadata.get("KnownStatus") or "RUNNING",
        "git_commit": os.getenv("GIT_COMMIT", "unknown"),
        "deploy_time": os.getenv("DEPLOY_TIME", "unknown"),
        "started_at": container_started_at[0] if container_started_at else PROCESS_STARTED_AT,
        "last_heartbeat": now,
        "status": "healthy",
        "cpu": {
            "percent": cpu_percent,
            "allocated_vcpus": allocated_vcpus,
        },
        "memory": {
            "used_bytes": memory_used,
            "limit_bytes": memory_limit,
            "percent": _percent(memory_used, memory_limit),
        },
        "disk": {
            "used_bytes": disk_used,
            "limit_bytes": disk_limit,
            "percent": _percent(disk_used, disk_limit),
        },
    }


def publish_backend_instance_metrics(
    redis_connection: Any,
    ttl_seconds: int,
    readiness_check: Callable[[], dict[str, str]] | None = None,
    history_hours: int = 12,
    history_bucket_seconds: int = 300,
) -> dict[str, Any]:
    snapshot = collect_backend_instance_metrics()
    dependencies = readiness_check() if readiness_check else {}
    is_ready = all(value == "ok" for value in dependencies.values())
    runtime_is_running = snapshot.get("runtime_status") == "RUNNING"
    snapshot["dependencies"] = dependencies
    snapshot["readiness"] = "ready" if is_ready else "not_ready"
    snapshot["status"] = "healthy" if is_ready and runtime_is_running else "unhealthy"
    key = f"{INSTANCE_KEY_PREFIX}{snapshot['instance_id']}"
    redis_connection.set(key, json.dumps(snapshot), ex=ttl_seconds)
    _record_backend_instance_history(
        redis_connection,
        snapshot,
        history_hours=max(1, history_hours),
        bucket_seconds=max(60, history_bucket_seconds),
    )
    return snapshot


def _record_backend_instance_history(
    redis_connection: Any,
    snapshot: dict[str, Any],
    history_hours: int,
    bucket_seconds: int,
) -> None:
    sampled_at = datetime.fromisoformat(snapshot["last_heartbeat"].replace("Z", "+00:00"))
    if sampled_at.tzinfo is None:
        sampled_at = sampled_at.replace(tzinfo=timezone.utc)
    sampled_at_epoch = int(sampled_at.timestamp())
    bucket_epoch = sampled_at_epoch - (sampled_at_epoch % bucket_seconds)
    bucket_time = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).isoformat()

    sample = {
        "timestamp": bucket_time,
        "instance_id": snapshot["instance_id"],
        "display_name": snapshot.get("display_name") or snapshot["instance_id"],
        "cpu_percent": (snapshot.get("cpu") or {}).get("percent"),
        "memory_percent": (snapshot.get("memory") or {}).get("percent"),
        "disk_percent": (snapshot.get("disk") or {}).get("percent"),
        "status": snapshot.get("status", "healthy"),
    }
    history_key = f"{HISTORY_KEY_PREFIX}{snapshot['instance_id']}"
    retention_seconds = history_hours * 3600 + bucket_seconds * 2
    cutoff_epoch = sampled_at_epoch - retention_seconds

    pipeline = redis_connection.pipeline(transaction=False)
    pipeline.zremrangebyscore(history_key, "-inf", cutoff_epoch)
    # Keep one representative value per five-minute bucket. Every heartbeat
    # refreshes the current bucket, so the chart shows its latest measurement.
    pipeline.zremrangebyscore(history_key, bucket_epoch, bucket_epoch)
    pipeline.zadd(history_key, {json.dumps(sample): bucket_epoch})
    pipeline.expire(history_key, retention_seconds)
    pipeline.execute()


def list_backend_instance_metrics(
    redis_connection: Any,
    stale_after_seconds: int,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    instances = []

    for key in redis_connection.scan_iter(match=f"{INSTANCE_KEY_PREFIX}*"):
        raw = redis_connection.get(key)
        if not raw:
            continue
        try:
            item = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            heartbeat = datetime.fromisoformat(item["last_heartbeat"].replace("Z", "+00:00"))
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            age_seconds = max(0, int((now - heartbeat).total_seconds()))
            item["heartbeat_age_seconds"] = age_seconds
            if age_seconds > stale_after_seconds:
                item["status"] = "stale"
            elif item.get("readiness") == "not_ready" or item.get("runtime_status") not in (None, "RUNNING"):
                item["status"] = "unhealthy"
            else:
                item["status"] = "healthy"
            instances.append(item)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignoring malformed backend instance heartbeat")

    instances.sort(key=lambda item: (item.get("status") != "healthy", item.get("display_name") or ""))
    healthy_count = sum(item["status"] == "healthy" for item in instances)
    return {
        "timestamp": now.isoformat(),
        "healthy_count": healthy_count,
        "total_instances": len(instances),
        "instances": instances,
    }


def list_backend_instance_history(
    redis_connection: Any,
    history_hours: int = 12,
    bucket_seconds: int = 300,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    hours = max(1, min(24, history_hours))
    cutoff_epoch = int((now.timestamp()) - hours * 3600)
    series = []

    for key in redis_connection.scan_iter(match=f"{HISTORY_KEY_PREFIX}*"):
        raw_samples = redis_connection.zrangebyscore(key, cutoff_epoch, "+inf")
        samples = []
        for raw in raw_samples:
            try:
                sample = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                samples.append(sample)
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning("Ignoring malformed backend instance history sample")
        if not samples:
            continue
        samples.sort(key=lambda item: item.get("timestamp") or "")
        latest = samples[-1]
        series.append({
            "instance_id": latest.get("instance_id"),
            "display_name": latest.get("display_name"),
            "samples": samples,
        })

    series.sort(key=lambda item: item.get("display_name") or "")
    return {
        "timestamp": now.isoformat(),
        "hours": hours,
        "bucket_seconds": max(60, bucket_seconds),
        "series": series,
    }


async def run_backend_instance_heartbeat(
    redis_connection: Any,
    interval_seconds: int,
    ttl_seconds: int,
    readiness_check: Callable[[], dict[str, str]] | None = None,
    history_hours: int = 12,
    history_bucket_seconds: int = 300,
) -> None:
    while True:
        try:
            await asyncio.to_thread(
                publish_backend_instance_metrics,
                redis_connection,
                ttl_seconds,
                readiness_check,
                history_hours,
                history_bucket_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to publish backend instance metrics")
        await asyncio.sleep(interval_seconds)
