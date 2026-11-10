"""Creator balances and manually processed withdrawals (USD integer cents)."""
import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
import models
from config import settings
from creator_referrals import utc

# Requests reserve the creator balance until an administrator records the Stripe transfer or rejects them.
# The PayPal-era statuses stay reserved too, so rows created before manual withdrawals are resolved the same way.
HELD = ("pending_review", "processing", "unclaimed", "on_hold", "manual_review")
COMMISSION_HOLD_DAYS = 3
PAYOUT_DAYS = 5  # We process withdrawal requests within this many days.


def now(): return datetime.now(timezone.utc)


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
    result = {name: getattr(payout, name) for name in ("id", "amount_cents", "fee_cents", "currency", "payout_email", "status", "created_at", "updated_at",
              "reviewed_at", "review_note", "paid_at")}
    result["net_cents"] = None if payout.fee_cents is None else payout.amount_cents - payout.fee_cents
    result["transfer_reference"] = payout.transfer_reference or payout.paypal_transaction_id
    result["due_at"] = utc(payout.created_at) + timedelta(days=PAYOUT_DAYS) if payout.created_at else None
    result["events"] = [{"status": row.status, "event": row.event, "created_at": row.created_at} for row in db.query(models.CreatorPayoutEvent)
                        .filter_by(payout_id=payout.id).order_by(models.CreatorPayoutEvent.created_at, models.CreatorPayoutEvent.id).all()]
    if admin:
        owner = db.get(models.User, payout.creator_user_id)
        result["creator_email"] = owner.email if owner else None
    return result


TRANSFER_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,99}")


def locked_payout(db, payout_id):
    initial = db.get(models.CreatorPayout, payout_id)
    if not initial: raise HTTPException(404, "creator_payout_not_found")
    lock_creator(db, initial.creator_user_id)
    return db.query(models.CreatorPayout).filter_by(id=payout_id).with_for_update().populate_existing().one()


def record_transfer(db, payout_id, admin_id, transfer_reference, fee_cents, note=""):
    """Record the Stripe transfer receipt. The creator's balance drops by the full requested
    amount; the fee comes out of that amount, so the creator receives amount - fee."""
    payout = locked_payout(db, payout_id)
    reference = transfer_reference.strip()
    if not TRANSFER_REFERENCE.fullmatch(reference): raise HTTPException(422, "creator_transfer_reference_invalid")
    if payout.status == "paid":
        if payout.transfer_reference == reference and payout.fee_cents == fee_cents: return payout
        raise HTTPException(409, "creator_payout_changed")
    if payout.status not in HELD: raise HTTPException(409, "creator_payout_changed")
    if fee_cents < 0 or fee_cents >= payout.amount_cents: raise HTTPException(422, "creator_fee_invalid")
    if balance(db, payout.creator_user_id)["adjustment_cents"]: raise HTTPException(409, "creator_insufficient_balance")
    if db.query(models.CreatorPayout.id).filter(models.CreatorPayout.transfer_reference == reference, models.CreatorPayout.id != payout.id).first():
        raise HTTPException(409, "creator_transfer_reference_used")
    current = now()
    payout.status, payout.transfer_reference, payout.fee_cents = "paid", reference, fee_cents
    payout.paid_at, payout.reviewed_by, payout.reviewed_at, payout.review_note = current, admin_id, current, note.strip() or None
    event(db, payout, "paid", admin_id)
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "creator_transfer_reference_used")
    return payout


def reject_request(db, payout_id, admin_id, note):
    payout = locked_payout(db, payout_id)
    if payout.status == "rejected": return payout
    if payout.status not in HELD: raise HTTPException(409, "creator_payout_changed")
    if not note.strip(): raise HTTPException(400, "creator_rejection_reason_required")
    payout.status, payout.reviewed_by, payout.reviewed_at, payout.review_note = "rejected", admin_id, now(), note.strip()
    event(db, payout, "rejected", admin_id)
    db.commit()
    return payout
