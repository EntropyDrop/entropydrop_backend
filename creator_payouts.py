"""Creator balances and durable, idempotent payout processing (USD integer cents)."""
import asyncio
import hashlib
import hmac
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from sqlalchemy import and_, func, or_
import models
from config import settings
from creator_referrals import utc
from database import SessionLocal
from paypal_payouts import create_batch, get_batch, PayoutTransportError

logger = logging.getLogger(__name__)
HELD = ("pending_review", "processing", "unclaimed", "on_hold", "manual_review")
RECONCILE = ("processing", "unclaimed", "on_hold", "paid")
COMMISSION_HOLD_DAYS = 3


def now(): return datetime.now(timezone.utc)


def configured():
    return bool(settings.PAYPAL_PAYOUTS_ENABLED and settings.PAYPAL_CLIENT_ID and settings.PAYPAL_SECRET
                and settings.PAYPAL_API_BASE.rstrip('/') in ("https://api-m.paypal.com", "https://api-m.sandbox.paypal.com"))


def email_address(value):
    email = value.strip().lower()
    if len(email) > 127 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", email):
        raise ValueError("Invalid email")
    return email


def masked_email(value):
    local, _, domain = (value or "").partition('@')
    return f"{local[:1]}***@{domain[:1]}***" if domain else "***"


def reference(owner_id, kind, value):
    digest = hmac.new(settings.JWT_SECRET_KEY.encode(), f"creator:{owner_id}:{kind}:{value}".encode(), hashlib.sha256).hexdigest()[:16].upper()
    return f"{kind.upper()}-{digest}"


def earned_condition():
    return and_(models.Order.status.in_(("paid", "shipping", "completed")), or_(models.Order.refund_status.is_(None), models.Order.refund_status == ""),
                or_(models.Order.figure_review_status.is_(None), models.Order.figure_review_status != "rejected"))


def eligibility_cutoff():
    return now() - timedelta(days=COMMISSION_HOLD_DAYS)


def eligible_condition():
    return and_(earned_condition(), models.Order.status == "completed", models.Order.paid_at <= eligibility_cutoff())


def balance(db, creator_id):
    orders = db.query(models.Order).filter(models.Order.creator_user_id == creator_id)
    earned = orders.filter(earned_condition()).with_entities(func.coalesce(func.sum(models.Order.creator_commission_cents), 0)).scalar()
    eligible = orders.filter(eligible_condition()).with_entities(func.coalesce(func.sum(models.Order.creator_commission_cents), 0)).scalar()
    payouts = db.query(models.CreatorPayout).filter_by(creator_user_id=creator_id)
    held = payouts.filter(models.CreatorPayout.status.in_(HELD)).with_entities(func.coalesce(func.sum(models.CreatorPayout.amount_cents), 0)).scalar()
    paid = payouts.filter_by(status="paid").with_entities(func.coalesce(func.sum(models.CreatorPayout.amount_cents), 0)).scalar()
    net = int(eligible - held - paid)
    return {"currency": "USD", "earned_cents": int(earned), "pending_cents": int(earned - eligible),
            "eligible_cents": int(eligible), "reserved_cents": int(held), "paid_cents": int(paid),
            "available_cents": max(0, net), "adjustment_cents": max(0, -net)}


def lock_creator(db, user_id):
    return db.query(models.User).filter_by(id=user_id).with_for_update().populate_existing().first()


def event(db, payout, name, actor=None):
    db.add(models.CreatorPayoutEvent(payout_id=payout.id, status=payout.status, event=name, actor_user_id=actor))


def payout_response(payout, db, admin=False):
    result = {name: getattr(payout, name) for name in ("id", "amount_cents", "currency", "paypal_email", "status", "created_at", "updated_at",
              "reviewed_at", "review_note", "paypal_batch_id", "paypal_transaction_id", "paypal_status", "last_error")}
    result["events"] = [{"status": row.status, "event": row.event, "created_at": row.created_at} for row in db.query(models.CreatorPayoutEvent)
                        .filter_by(payout_id=payout.id).order_by(models.CreatorPayoutEvent.created_at, models.CreatorPayoutEvent.id).all()]
    if admin:
        owner = db.get(models.User, payout.creator_user_id)
        result["creator_email"] = owner.email if owner else None
    return result


def apply_paypal_result(payout, data):
    header = data.get("batch_header", {})
    batch_id = header.get("payout_batch_id")
    if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", batch_id):
        raise PayoutTransportError("paypal_invalid_batch")
    sender_id = header.get("sender_batch_header", {}).get("sender_batch_id")
    if sender_id != f"creator-{payout.id}" or (payout.paypal_batch_id and batch_id != payout.paypal_batch_id):
        raise PayoutTransportError("paypal_mismatch")
    payout.paypal_batch_id = batch_id
    items = data.get("items", [])
    if not items:
        # A successful batch is not proof that its recipient was paid.
        if payout.status != "paid" and header.get("batch_status") in ("DENIED", "CANCELED"):
            payout.status = "failed"
            payout.paypal_status = header["batch_status"]
        return
    if len(items) != 1:
        raise PayoutTransportError("paypal_mismatch")
    item, expected = items[0], Decimal(payout.amount_cents) / 100
    recipient = item.get("payout_item", {})
    amount = recipient.get("amount", {})
    try: received = Decimal(str(amount.get("value", "")))
    except InvalidOperation: raise PayoutTransportError("paypal_mismatch")
    if (recipient.get("sender_item_id") != payout.id or recipient.get("receiver", "").lower() != payout.paypal_email
        or amount.get("currency") != "USD" or not received.is_finite() or received != expected):
        raise PayoutTransportError("paypal_mismatch")
    status = item.get("transaction_status")
    mapped = {"SUCCESS": "paid", "PENDING": "processing", "UNCLAIMED": "unclaimed", "ONHOLD": "on_hold", "BLOCKED": "on_hold",
              "FAILED": "failed", "DENIED": "failed", "RETURNED": "returned", "REFUNDED": "returned", "REVERSED": "returned", "CANCELED": "returned"}
    if status not in mapped: raise PayoutTransportError("paypal_unknown_status")
    if payout.status == "paid" and mapped[status] not in ("paid", "returned"):
        return  # Never let a delayed pending/failure response reopen a paid balance.
    payout.status, payout.paypal_status = mapped[status], status
    payout.paypal_item_id = item.get("payout_item_id") or payout.paypal_item_id
    payout.paypal_transaction_id = item.get("transaction_id") or payout.paypal_transaction_id


def process_payout(db, payout_id, force=False):
    """Commit a lease before I/O. Crashes/retries always retain the same sender ID."""
    if not configured(): return
    initial = db.get(models.CreatorPayout, payout_id)
    if not initial: return
    lock_creator(db, initial.creator_user_id)
    payout = db.query(models.CreatorPayout).filter_by(id=payout_id).with_for_update().populate_existing().one()
    current = now()
    if payout.status not in RECONCILE + ("manual_review",) or (payout.processing_until and utc(payout.processing_until) > current):
        db.rollback(); return
    if not force and payout.next_check_at and utc(payout.next_check_at) > current:
        db.rollback(); return
    if not payout.paypal_batch_id and payout.first_sent_at and utc(payout.first_sent_at) < current - timedelta(days=29):
        payout.status = "manual_review"; payout.last_error = "paypal_retry_window_expired"
        event(db, payout, payout.last_error); db.commit(); return
    if payout.status == "manual_review" and not payout.paypal_batch_id:
        db.rollback(); return
    if not payout.first_sent_at:
        if balance(db, payout.creator_user_id)["adjustment_cents"]:
            payout.status = "rejected"; payout.review_note = "balance_changed"
            event(db, payout, "balance_changed"); db.commit(); return
        payout.first_sent_at = current
    payout.processing_until = current + timedelta(seconds=90)
    payout.attempt_count += 1
    attempt = payout.attempt_count
    batch_id, email, cents = payout.paypal_batch_id, payout.paypal_email, payout.amount_cents
    db.commit()
    error, definitive, data = None, False, None
    try:
        data = get_batch(batch_id) if batch_id else create_batch(payout_id, cents, email)
    except PayoutTransportError as exc:
        error, definitive = exc.code, exc.definitive
    except Exception:
        error = "paypal_unconfirmed"
    lock_creator(db, initial.creator_user_id)
    payout = db.query(models.CreatorPayout).filter_by(id=payout_id).with_for_update().populate_existing().one()
    if payout.attempt_count != attempt:
        db.rollback(); return
    previous = payout.status, payout.last_error
    if data is not None:
        try: apply_paypal_result(payout, data)
        except (PayoutTransportError, TypeError, AttributeError, ValueError) as exc:
            error = exc.code if isinstance(exc, PayoutTransportError) else "paypal_mismatch"
    if payout.status != "paid" and error in ("paypal_mismatch", "paypal_invalid_batch", "paypal_duplicate_unresolved", "paypal_unknown_status"):
        payout.status = "manual_review"
    elif definitive and not batch_id and attempt == 1:
        # A rejection after an earlier timeout cannot disprove that the earlier
        # submission succeeded. Keep that money reserved until reconciled.
        payout.status = "failed"
    payout.last_error = error
    payout.processing_until = None
    interval = 86400 if payout.status == "paid" else min(900, 30 * 2 ** min(attempt, 5))
    payout.next_check_at = now() + timedelta(seconds=interval)
    if previous != (payout.status, payout.last_error): event(db, payout, error or "paypal_status_updated")
    db.commit()


def reconcile_due():
    if not configured(): return
    with SessionLocal() as db:
        ids = db.query(models.CreatorPayout.id).filter(models.CreatorPayout.status.in_(RECONCILE),
              or_(models.CreatorPayout.next_check_at.is_(None), models.CreatorPayout.next_check_at <= now())).order_by(models.CreatorPayout.next_check_at).limit(50).all()
    for (payout_id,) in ids:
        with SessionLocal() as db:
            try: process_payout(db, payout_id)
            except Exception:
                db.rollback(); logger.exception("Creator payout reconciliation failed for %s", payout_id)


async def start_payout_reconciliation_job():
    while True:
        await asyncio.to_thread(reconcile_due)
        await asyncio.sleep(30)
