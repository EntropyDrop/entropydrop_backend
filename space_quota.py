"""Transactional quota helpers shared by the Space write surfaces."""

from __future__ import annotations

import datetime
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session, load_only

from space import models


UTC = datetime.timezone.utc
UTC_DAY_SECONDS = 86_400
USAGE_CLEANUP_INTERVAL_SECONDS = 3_600
USAGE_CLEANUP_RETENTION_SECONDS = 2 * UTC_DAY_SECONDS
USAGE_CLEANUP_BATCH_SIZE = 10_000
_cleanup_lock = threading.Lock()
_last_cleanup_at = 0.0


@dataclass(frozen=True)
class QuotaWindow:
    seconds: int
    limit: int
    label: str


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def bucket_start(now: datetime.datetime, window_seconds: int) -> datetime.datetime:
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    timestamp = int(_utc(now).timestamp())
    return datetime.datetime.fromtimestamp(
        timestamp - timestamp % window_seconds,
        tz=UTC,
    )


def _bucket(
    db: Session,
    *,
    principal_id: str,
    scope_id: str,
    metric: str,
    window_seconds: int,
    now: datetime.datetime,
    lock: bool = False,
) -> models.SpaceUsageBucket | None:
    query = db.query(models.SpaceUsageBucket).filter(
        models.SpaceUsageBucket.principal_id == principal_id,
        models.SpaceUsageBucket.scope_id == scope_id,
        models.SpaceUsageBucket.metric == metric,
        models.SpaceUsageBucket.window_seconds == window_seconds,
        models.SpaceUsageBucket.bucket_start == bucket_start(now, window_seconds),
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def usage(
    db: Session,
    *,
    principal_id: str,
    scope_id: str,
    metric: str,
    window_seconds: int,
    now: datetime.datetime | None = None,
) -> int:
    current = now or datetime.datetime.now(UTC)
    row = _bucket(
        db,
        principal_id=principal_id,
        scope_id=scope_id,
        metric=metric,
        window_seconds=window_seconds,
        now=current,
    )
    return int(row.used or 0) if row is not None else 0


def ensure_usage_floor(
    db: Session,
    *,
    principal_id: str,
    scope_id: str,
    metric: str,
    window_seconds: int,
    floor: int,
    now: datetime.datetime | None = None,
) -> int:
    current = now or datetime.datetime.now(UTC)
    normalized_floor = max(0, int(floor))
    row = _bucket(
        db,
        principal_id=principal_id,
        scope_id=scope_id,
        metric=metric,
        window_seconds=window_seconds,
        now=current,
        lock=True,
    )
    if row is None:
        row = models.SpaceUsageBucket(
            principal_id=principal_id,
            scope_id=scope_id,
            metric=metric,
            window_seconds=window_seconds,
            bucket_start=bucket_start(current, window_seconds),
            used=normalized_floor,
        )
        db.add(row)
        db.flush()
    elif int(row.used or 0) < normalized_floor:
        row.used = normalized_floor
    return int(row.used or 0)


def _maybe_cleanup_expired_buckets(db: Session, now: datetime.datetime) -> None:
    """Bound retention for the short-lived counters without delaying every write."""
    global _last_cleanup_at
    monotonic_now = time.monotonic()
    if monotonic_now - _last_cleanup_at < USAGE_CLEANUP_INTERVAL_SECONDS:
        return
    with _cleanup_lock:
        monotonic_now = time.monotonic()
        if monotonic_now - _last_cleanup_at < USAGE_CLEANUP_INTERVAL_SECONDS:
            return
        _last_cleanup_at = monotonic_now
    cutoff = _utc(now) - datetime.timedelta(seconds=USAGE_CLEANUP_RETENTION_SECONDS)
    rows = db.query(models.SpaceUsageBucket).options(load_only(
        models.SpaceUsageBucket.principal_id,
        models.SpaceUsageBucket.scope_id,
        models.SpaceUsageBucket.metric,
        models.SpaceUsageBucket.window_seconds,
        models.SpaceUsageBucket.bucket_start,
    )).filter(
        models.SpaceUsageBucket.window_seconds <= UTC_DAY_SECONDS,
        models.SpaceUsageBucket.bucket_start < cutoff,
    ).order_by(models.SpaceUsageBucket.bucket_start).limit(USAGE_CLEANUP_BATCH_SIZE).all()
    for row in rows:
        db.delete(row)


def reserve(
    db: Session,
    *,
    principal_id: str,
    scope_id: str,
    metric: str,
    amount: int,
    windows: tuple[QuotaWindow, ...],
    code: str,
    message: str,
    now: datetime.datetime | None = None,
) -> dict[int, int]:
    """Reserve all windows or raise before changing any of them.

    Callers serialize missing-row creation by locking the owning user/world row
    first, and commit this reservation in the same transaction as the write.
    """
    current = now or datetime.datetime.now(UTC)
    _maybe_cleanup_expired_buckets(db, current)
    normalized_amount = int(amount)
    if normalized_amount < 0:
        raise ValueError("quota amount cannot be negative")
    if normalized_amount == 0:
        return {window.seconds: usage(
            db,
            principal_id=principal_id,
            scope_id=scope_id,
            metric=metric,
            window_seconds=window.seconds,
            now=current,
        ) for window in windows}

    rows: list[tuple[QuotaWindow, models.SpaceUsageBucket | None, int]] = []
    for window in windows:
        if window.seconds <= 0 or window.limit <= 0:
            raise ValueError("quota windows and limits must be positive")
        row = _bucket(
            db,
            principal_id=principal_id,
            scope_id=scope_id,
            metric=metric,
            window_seconds=window.seconds,
            now=current,
            lock=True,
        )
        used = int(row.used or 0) if row is not None else 0
        if used + normalized_amount > window.limit:
            start = bucket_start(current, window.seconds)
            reset_at = start + datetime.timedelta(seconds=window.seconds)
            retry_after = max(1, int((reset_at - current).total_seconds()) + 1)
            raise HTTPException(
                status_code=429,
                detail={
                    "code": code,
                    "message": message,
                    "metric": metric,
                    "window": window.label,
                    "limit": window.limit,
                    "used": used,
                    "requested": normalized_amount,
                    "remaining": max(0, window.limit - used),
                    "reset_at": reset_at.isoformat(),
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )
        rows.append((window, row, used))

    result: dict[int, int] = {}
    for window, row, used in rows:
        if row is None:
            row = models.SpaceUsageBucket(
                principal_id=principal_id,
                scope_id=scope_id,
                metric=metric,
                window_seconds=window.seconds,
                bucket_start=bucket_start(current, window.seconds),
                used=used + normalized_amount,
            )
            db.add(row)
        else:
            row.used = used + normalized_amount
        result[window.seconds] = used + normalized_amount
    # Space sessions deliberately disable SQLAlchemy autoflush. Flush the
    # reservation so quota responses and later reservations in this same
    # transaction observe the amount without committing the protected write.
    db.flush()
    return result
