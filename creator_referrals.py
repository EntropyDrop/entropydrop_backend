"""Creator attribution and server-priced discounts for physical figure orders."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import re

import jwt
from fastapi import HTTPException

import models
from config import settings

# Limit how long an anonymous visit receipt can bind a source to an account.
# Once bound, the account's default persists while the creator code is active.
REFERRAL_RECEIPT_DAYS = 30
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,31}$")


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def normalize_code(value):
    code = value.strip().upper() if isinstance(value, str) else ""
    if not CODE_RE.fullmatch(code):
        raise HTTPException(400, "creator_code_invalid")
    return code


def active_code(db, value, *, required=True):
    try:
        code = db.query(models.CreatorCode).filter_by(code=normalize_code(value)).first()
    except HTTPException:
        code = None
    if code is None or not code.creator_user_id or not code.active or (code.expires_at and utc(code.expires_at) <= datetime.now(timezone.utc)):
        if required:
            raise HTTPException(400, "creator_code_invalid")
        return None
    return code


def visit_receipt(code):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=REFERRAL_RECEIPT_DAYS)
    token = jwt.encode({"aud": "creator-referral", "code": code.code, "iat": int(now.timestamp()), "exp": int(expires.timestamp())},
                       settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return {"token": token, "code": code.code, "expires_at": expires.isoformat()}


def bind_referral(db, user_id, token):
    """Receipts are signed and time-limited; repeating one never extends its window."""
    if not token:
        return
    try:
        data = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience="creator-referral",
                          options={"require": ["exp", "iat", "code"]})
        clicked = datetime.fromtimestamp(data["iat"], timezone.utc)
        expires = datetime.fromtimestamp(data["exp"], timezone.utc)
        code = active_code(db, data["code"], required=False)
        if not code or code.creator_user_id == user_id or expires > clicked + timedelta(days=REFERRAL_RECEIPT_DAYS):
            return
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
        return
    # Lock the parent even before the first referral row exists, so parallel
    # login/checkout requests cannot overwrite the original acquisition source.
    db.query(models.User).filter_by(id=user_id).with_for_update().first()
    saved = db.query(models.UserCreatorReferral).filter_by(user_id=user_id).populate_existing().first()
    if saved is None:
        db.add(models.UserCreatorReferral(user_id=user_id, original_code=code.code, original_at=clicked,
               current_code=code.code, current_clicked_at=clicked, current_expires_at=expires))
    elif clicked >= utc(saved.current_clicked_at):
        saved.current_code = code.code
        saved.current_clicked_at = clicked
        saved.current_expires_at = expires
    db.flush()


def default_code(db, user_id):
    """Bound sources persist; only the creator code's own validity can expire."""
    saved = db.query(models.UserCreatorReferral).filter_by(user_id=user_id).first()
    if saved:
        code = active_code(db, saved.current_code, required=False)
        return code if code and code.creator_user_id != user_id else None
    return None


def select_offer(db, user_id, value, version=None):
    code = default_code(db, user_id) if value is None else active_code(db, value) if value.strip() else None
    if code and code.creator_user_id == user_id:
        raise HTTPException(400, "creator_self_referral")
    if code and version is not None and code.version != version:
        raise HTTPException(409, "creator_offer_changed")
    return {"code": code.code, "creator_user_id": code.creator_user_id, "discount_bps": code.discount_bps,
            "commission_bps": code.commission_bps, "version": code.version} if code else None


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price_offer(subtotal, shipping, offer, global_discount_bps=0):
    subtotal, shipping = money(subtotal), money(shipping)
    global_discount = money(subtotal * Decimal(global_discount_bps) / 10000)
    after_global = subtotal - global_discount
    creator_discount = money(after_global * Decimal(offer["discount_bps"]) / 10000) if offer else Decimal(0)
    discount = global_discount + creator_discount
    net = subtotal - discount
    commission = money(net * Decimal(offer["commission_bps"]) / 10000) if offer else Decimal(0)
    return float(discount), float(net + shipping), float(commission)


def reprice_order(order):
    order.discount_amount, order.total_price, order.creator_commission = price_offer(order.price, order.shipping_fee, order.creator_snapshot, order.global_discount_bps or 0)
    order.creator_commission_cents = int(money(order.creator_commission) * 100)


def set_order_offer(order, offer):
    order.creator_snapshot = offer
    order.creator_code = offer["code"] if offer else None
    order.creator_user_id = offer.get("creator_user_id") if offer else None
    reprice_order(order)


def public_offer(offer):
    return {"code": offer["code"],
            "discount_percent": offer["discount_bps"] / 100, "version": offer["version"]} if offer else None


def ensure_discounted_items_editable(order):
    if order.status != "pending_payment" or order.paypal_order_id or order.capture_started:
        raise HTTPException(409, "creator_code_locked")
