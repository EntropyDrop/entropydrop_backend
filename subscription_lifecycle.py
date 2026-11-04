"""Serialize contract changes on the owner row; keep external operation intents durable."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import func

import models
import payment_utils

logger = logging.getLogger(__name__)
TERMINAL = {"CANCELLED", "EXPIRED"}
PROVIDER_STATUSES = TERMINAL | {"APPROVAL_PENDING", "APPROVED", "ACTIVE", "SUSPENDED"}


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def paypal_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def lock_user(db, user_id):
    user = db.query(models.User).filter_by(id=user_id).with_for_update().populate_existing().one()
    # Webhook owner lookup can have loaded a contract before waiting on this
    # lock. Refresh that identity-map snapshot after the preceding writer commits.
    db.query(models.PaypalSubscription).filter_by(user_id=user_id).populate_existing().all()
    return user


def contracts(db, user):
    # Also supports old rows created during a rolling migration.
    if user.paypal_subscription_id and not db.query(models.PaypalSubscription).filter_by(paypal_id=user.paypal_subscription_id).first():
        db.add(models.PaypalSubscription(user_id=user.id, paypal_id=user.paypal_subscription_id,
            status=user.paypal_subscription_status or "UNKNOWN", created_at=user.created_at or now()))
        db.flush()
    return db.query(models.PaypalSubscription).filter_by(user_id=user.id).order_by(models.PaypalSubscription.created_at).all()


def display(user, contract):
    if user.paypal_subscription_id == contract.paypal_id:
        # Keep a usable cancellation control while cancellation is unresolved.
        user.paypal_subscription_status = contract.status


def refresh_display(db, user):
    rows = contracts(db, user)
    if any(row.cancel_requested_at and not row.cancelled_at for row in rows):
        user.paypal_subscription_status = "CANCELLATION_PENDING"
    else:
        for row in rows:
            display(user, row)


def remember(db, user, sub_id, data):
    if not isinstance(sub_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", sub_id):
        raise HTTPException(400, "Invalid subscription ID")
    if data.get("id", sub_id) != sub_id:
        raise HTTPException(409, "PayPal subscription ID mismatch")
    if data.get("custom_id") and data["custom_id"] != user.id:
        raise HTTPException(403, "Subscription does not belong to current user")
    status = data.get("status")
    if status not in PROVIDER_STATUSES:
        raise HTTPException(503, "PayPal subscription status is unavailable")
    contracts(db, user)
    row = db.query(models.PaypalSubscription).filter_by(paypal_id=sub_id).first()
    if row and row.user_id != user.id:
        raise HTTPException(403, "Subscription is already linked to another user")
    if not row:
        row = models.PaypalSubscription(user_id=user.id, paypal_id=sub_id, status="UNKNOWN",
                                       created_at=paypal_time(data.get("create_time")) or now())
        db.add(row)
        db.flush()
    apply_status(user, row, data)
    return row


def apply_status(user, row, data):
    if data.get("id", row.paypal_id) != row.paypal_id or (data.get("custom_id") and data["custom_id"] != user.id):
        raise HTTPException(409, "PayPal subscription ownership mismatch")
    status = data.get("status")
    if status not in PROVIDER_STATUSES:
        raise HTTPException(503, "PayPal subscription status is unavailable")
    changed_at = paypal_time(data.get("status_update_time"))
    # A provider-confirmed terminal contract cannot be resurrected by a stale GET/event.
    if row.cancelled_at or (status not in TERMINAL and changed_at and row.provider_updated_at and changed_at < utc(row.provider_updated_at)):
        display(user, row)
        return
    row.status = status
    row.plan_id = data.get("plan_id") or row.plan_id
    row.provider_updated_at = changed_at or row.provider_updated_at
    if status in TERMINAL:
        row.cancelled_at = changed_at or now()
        row.next_check_at = None
        row.last_error = None
    row.approval_url = next((link["href"] for link in data.get("links", []) if link.get("rel") == "approve"), row.approval_url)
    display(user, row)


def request_cancel(row):
    row.cancel_requested_at = row.cancel_requested_at or now()
    if not row.cancelled_at:
        row.next_check_at = now()


def recover_create(db, user, row):
    """Replay the exact durable request only within PayPal's 72-hour retention period."""
    if row.paypal_id:
        return row
    if not row.request_id or not row.create_requested_at or now() - utc(row.create_requested_at) >= timedelta(hours=71):
        row.last_error = "creation_requires_manual_reconciliation"
        raise HTTPException(409, "Subscription creation needs payment reconciliation; contact support")
    data = payment_utils.create_paypal_subscription_api(plan_id=row.plan_id, custom_id=user.id,
        return_url=row.return_url, cancel_url=row.return_url, request_id=row.request_id)
    sub_id = data.get("id")
    if not isinstance(sub_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", sub_id):
        raise HTTPException(503, "PayPal subscription ID is unavailable")
    if data.get("custom_id", user.id) != user.id or data.get("plan_id", row.plan_id) != row.plan_id:
        raise HTTPException(409, "PayPal subscription creation mismatch")
    existing = db.query(models.PaypalSubscription).filter_by(paypal_id=sub_id).first()
    if existing and existing.id != row.id:
        if existing.user_id != user.id:
            raise HTTPException(403, "Subscription belongs to another user")
        # A webhook may have recovered a timed-out create before this retry.
        request_id, row.request_id = row.request_id, None
        db.flush()
        existing.request_id = request_id
        existing.return_url = row.return_url
        existing.create_requested_at = row.create_requested_at
        existing.cancel_requested_at = existing.cancel_requested_at or row.cancel_requested_at
        db.delete(row)
        db.flush()
        row = existing
    else:
        row.paypal_id = sub_id
    apply_status(user, row, data)
    db.flush()
    return row


def cancel_one(db, user, row):
    """Caller has persisted cancel intent and holds the user lock. Never report uncertain success."""
    try:
        row = recover_create(db, user, row)
        if row.cancelled_at:
            return True
        data = payment_utils.get_paypal_subscription_api(row.paypal_id)
        if data.get("id", row.paypal_id) != row.paypal_id:
            raise ValueError("subscription_id_mismatch")
        apply_status(user, row, data)
        if row.status not in TERMINAL:
            try:
                payment_utils.cancel_paypal_subscription_api(row.paypal_id, reason="User cancelled via dashboard")
            except Exception:
                # A timeout may follow a successful provider cancellation.
                apply_status(user, row, payment_utils.get_paypal_subscription_api(row.paypal_id))
                if not row.cancelled_at:
                    raise
            else:
                apply_status(user, row, {"status": "CANCELLED"})
        row.last_error = None
        row.next_check_at = None
        return True
    except Exception as exc:
        row.last_error = row.last_error or "cancellation_unconfirmed"
        row.next_check_at = now() + timedelta(minutes=1)
        logger.warning("Subscription cancellation unconfirmed: %s (%s)", row.id, type(exc).__name__)
        return False


def cancel_all(db, user_id):
    user = lock_user(db, user_id)
    rows = contracts(db, user)
    user.subscription_cancel_requested_at = now()
    for row in rows:
        request_cancel(row)
    refresh_display(db, user)
    db.commit()  # survives process failure or PayPal timeout
    user = lock_user(db, user_id)
    complete = True
    for row in contracts(db, user):
        if row.cancel_requested_at:
            complete = cancel_one(db, user, row) and complete
    refresh_display(db, user)
    db.commit()
    if not complete:
        raise HTTPException(503, "Cancellation is not yet confirmed by PayPal; we will keep retrying")
    return {"status": "success", "message": "Subscription cancelled successfully"}


def select_contract(db, user, row, *, explicit=False):
    """Never displace another live contract; stop duplicate or pre-cancellation approvals."""
    cutoff = utc(user.subscription_cancel_requested_at)
    if cutoff and utc(row.created_at) <= cutoff:
        request_cancel(row)
    current = next((r for r in contracts(db, user) if r.paypal_id and r.paypal_id == user.paypal_subscription_id), None)
    if row.status not in TERMINAL and current and current.id != row.id:
        if not current.cancelled_at:
            apply_status(user, current, payment_utils.get_paypal_subscription_api(current.paypal_id))
        if current.status not in TERMINAL:
            request_cancel(row)
    if row.cancel_requested_at and not row.cancelled_at:
        user_id, row_id = user.id, row.id
        db.commit()
        user = lock_user(db, user_id)
        row = db.get(models.PaypalSubscription, row_id)
        confirmed = cancel_one(db, user, row)
        refresh_display(db, user)
        db.commit()
        user = lock_user(db, user_id)
        if not confirmed:
            raise HTTPException(503, "Subscription cancellation is pending confirmation")
    if row.cancel_requested_at:
        refresh_display(db, user)
        return False
    if not current or current.id == row.id or (row.status not in TERMINAL and current.status in TERMINAL
            and (explicit or utc(row.created_at) > utc(current.created_at))):
        user.paypal_subscription_id = row.paypal_id
        display(user, row)
    refresh_display(db, user)
    return row.status not in TERMINAL


def create_checkout(db, user_id, plan_id, return_url):
    if not plan_id:
        raise HTTPException(503, "Subscription plan is not configured")
    user = lock_user(db, user_id)
    rows = contracts(db, user)
    for row in rows:
        if row.cancel_requested_at and not row.cancelled_at:
            raise HTTPException(409, "Previous subscription cancellation is still being confirmed")
        if not row.paypal_id:
            row = recover_create(db, user, row)
        if row.paypal_id and not row.cancelled_at:
            apply_status(user, row, payment_utils.get_paypal_subscription_api(row.paypal_id))
    rows = contracts(db, user)
    live = [row for row in rows if not row.cancelled_at]
    if len(live) > 1:
        db.commit()
        raise HTTPException(409, "Multiple subscriptions found; cancel existing subscriptions before subscribing again")
    row = live[0] if live else None
    if row and row.status in {"ACTIVE", "SUSPENDED"}:
        user.paypal_subscription_id = row.paypal_id
        display(user, row)
        if row.status == "SUSPENDED":
            db.commit()
            raise HTTPException(409, "Cancel the suspended subscription before starting another")
        data = payment_utils.revise_paypal_subscription_api(row.paypal_id, plan_id)
        approval = next((link["href"] for link in data.get("links", []) if link.get("rel") == "approve"), None)
        db.commit()
        if not approval:
            raise HTTPException(409, "Subscription is already active; refresh your account")
        return {"approval_url": approval, "subscription_id": row.paypal_id}
    if row and row.plan_id != plan_id:
        db.commit()
        raise HTTPException(409, "Finish or cancel the existing subscription checkout before changing plans")
    if not row:
        row = models.PaypalSubscription(user_id=user.id, plan_id=plan_id, request_id=str(uuid.uuid4()),
            return_url=return_url, created_at=now(), create_requested_at=now())
        db.add(row)
        db.flush()
    row_id = row.id
    db.commit()  # reserve the request ID BEFORE contacting PayPal
    user = lock_user(db, user_id)
    row = db.get(models.PaypalSubscription, row_id)
    try:
        row = recover_create(db, user, row)
        if not select_contract(db, user, row, explicit=True):
            db.commit()
            raise HTTPException(409, "This checkout was cancelled; start a new subscription")
        db.commit()  # save the PayPal ID even when its response lacks an approval URL
        if not row.approval_url:
            raise HTTPException(409, "Subscription approval is pending; refresh your account")
        return {"approval_url": row.approval_url, "subscription_id": row.paypal_id}
    except HTTPException:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(503, "Subscription creation is unconfirmed; retry the same checkout shortly")


def reconcile_cancellations():
    from database import SessionLocal
    with SessionLocal() as db:
        user_ids = [r[0] for r in db.query(models.PaypalSubscription.user_id).filter(
            models.PaypalSubscription.cancel_requested_at.isnot(None),
            models.PaypalSubscription.cancelled_at.is_(None),
            models.PaypalSubscription.next_check_at <= now())
            .group_by(models.PaypalSubscription.user_id)
            .order_by(func.min(models.PaypalSubscription.next_check_at)).limit(20).all()]
    for user_id in user_ids:
        with SessionLocal() as db:
            try:
                user = lock_user(db, user_id)
                for row in contracts(db, user):
                    if row.cancel_requested_at and not row.cancelled_at:
                        cancel_one(db, user, row)
                refresh_display(db, user)
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Subscription cancellation reconciliation failed for user %s", user_id)


async def start_subscription_reconciliation_job():
    while True:
        await asyncio.to_thread(reconcile_cancellations)
        await asyncio.sleep(60)
