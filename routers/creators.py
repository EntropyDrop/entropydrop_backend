from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy import func
from uuid import UUID

import creator_payouts as payouts

import auth
import models
from creator_referrals import active_code, bind_referral, normalize_code, price_offer, public_offer, select_offer, utc, visit_receipt
from database import get_db
from rate_limit import limiter

router = APIRouter(prefix="/api/creators", tags=["creators"])


class CodeRequest(BaseModel):
    code: str = Field(max_length=32)


class ReferralRequest(BaseModel):
    token: str = Field(max_length=2048)


class QuoteRequest(BaseModel):
    code: str | None = Field(None, max_length=32)
    model_type: str = Field(max_length=100)
    quantity: int = Field(1, ge=1, le=10, strict=True)
    referral_token: str | None = Field(None, max_length=2048)


class CreatorSettings(BaseModel):
    creator_email: str = Field(min_length=3, max_length=100)
    discount_bps: int = Field(ge=0, le=9900, strict=True)
    commission_bps: int = Field(ge=0, le=10000, strict=True)
    active: bool = True
    expires_at: datetime | None = None
    expected_version: int | None = Field(None, ge=1, strict=True)
    model_config = ConfigDict(extra="forbid")

    @field_validator("creator_email")
    @classmethod
    def validate_email(cls, value):
        return payouts.email_address(value)

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value and value.tzinfo is None:
            raise ValueError("Expiry must include a timezone")
        return value


@router.post("/visit")
@limiter.limit("30/minute", override_defaults=False)
def record_visit(request: Request, req: CodeRequest, db: Session = Depends(get_db)):
    return visit_receipt(active_code(db, req.code))


@router.post("/referral")
def save_referral(req: ReferralRequest, db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_user)):
    bind_referral(db, user.id, req.token)
    db.commit()
    return {"saved": True}


@router.post("/quote")
def quote(req: QuoteRequest, db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_user)):
    from routers.order import _model_shipping_fee
    bind_referral(db, user.id, req.referral_token)
    product = db.query(models.ModelSalesLimit).filter_by(model_type=req.model_type, order_type="print").first()
    if not product or product.stock < req.quantity:
        raise HTTPException(409, "creator_product_unavailable")
    offer = select_offer(db, user.id, req.code)
    subtotal = float(product.price) * req.quantity
    shipping = _model_shipping_fee("print", product.model_type)
    discount, total, _ = price_offer(subtotal, shipping, offer)
    db.commit()
    return {"offer": public_offer(offer), "subtotal": subtotal, "shipping_fee": shipping, "discount_amount": discount, "total_price": total}


def settings_response(code, db):
    orders = db.query(models.Order).filter_by(creator_code=code.code).all()
    earned = [order for order in orders if order.status in ("paid", "shipping", "completed")
              and not order.refund_status and order.figure_review_status != "rejected"]
    cutoff = payouts.eligibility_cutoff()
    eligible = [order for order in earned if order.status == "completed" and order.paid_at and utc(order.paid_at) <= cutoff]
    owner = db.get(models.User, code.creator_user_id) if code.creator_user_id else None
    return {"code": code.code, "creator_email": owner.email if owner else None, "creator_user_id": code.creator_user_id, "discount_bps": code.discount_bps,
            "commission_bps": code.commission_bps, "active": code.active, "expires_at": code.expires_at,
            "version": code.version, "attributed_orders": len(orders),
            "acquired_users": db.query(models.UserCreatorReferral).filter_by(original_code=code.code).count(),
            "paid_sales": round(sum(order.price - order.discount_amount for order in earned), 2),
            "pending_commission": round(sum(order.creator_commission for order in earned if order not in eligible), 2),
            "eligible_commission": round(sum(order.creator_commission for order in eligible), 2)}


@router.get("/manage", dependencies=[Depends(auth.get_current_admin)])
def list_codes(db: Session = Depends(get_db)):
    return [settings_response(code, db) for code in db.query(models.CreatorCode).order_by(models.CreatorCode.created_at.desc()).all()]


@router.put("/manage/{value}", dependencies=[Depends(auth.get_current_admin)])
def save_code(value: str, req: CreatorSettings, db: Session = Depends(get_db)):
    code = normalize_code(value)
    owner = db.query(models.User).filter(func.lower(models.User.email) == req.creator_email).first()
    if not owner:
        raise HTTPException(404, "creator_user_not_found")
    record = db.query(models.CreatorCode).filter_by(code=code).with_for_update().populate_existing().first()
    if req.expected_version is None:
        if record: raise HTTPException(409, "creator_code_exists")
        record = models.CreatorCode(code=code, creator_user_id=owner.id)
        db.add(record)
    else:
        if not record or record.version != req.expected_version:
            raise HTTPException(409, "creator_offer_changed")
        if record.creator_user_id and record.creator_user_id != owner.id:
            raise HTTPException(409, "creator_owner_locked")
        record.version += 1
        record.creator_user_id = owner.id
    owner.is_creator = True
    for key, field in req.model_dump(exclude={"expected_version", "creator_email"}).items():
        setattr(record, key, field)
    # Legacy codes can be bound exactly once. Never move historical earnings
    # between accounts, and never pay creators for their own purchases.
    for order in db.query(models.Order).filter_by(creator_code=code, creator_user_id=None).with_for_update().all():
        order.creator_user_id = owner.id
        if order.user_id == owner.id:
            order.creator_commission = 0; order.creator_commission_cents = 0
        if order.creator_snapshot:
            order.creator_snapshot = {**order.creator_snapshot, "creator_user_id": owner.id}
    from sqlalchemy.exc import IntegrityError
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, "creator_code_exists")
    return settings_response(record, db)


def require_creator(user: models.User = Depends(auth.get_current_user)):
    if not user.is_creator: raise HTTPException(403, "creator_required")
    return user


def paginated(query, page, page_size, serialize):
    total = query.count()
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [serialize(row) for row in rows], "total": total, "page": page, "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size)}


@router.get("/me")
def creator_dashboard(db: Session = Depends(get_db), user: models.User = Depends(require_creator)):
    codes = db.query(models.CreatorCode).filter_by(creator_user_id=user.id).order_by(models.CreatorCode.code).all()
    return {"balance": payouts.balance(db, user.id), "payouts_enabled": payouts.configured(),
            "codes": [{"code": code.code, "active": code.active, "usable": code.active and (not code.expires_at or utc(code.expires_at) > payouts.now()), "expires_at": code.expires_at,
                       "discount_percent": code.discount_bps / 100, "commission_percent": code.commission_bps / 100} for code in codes]}


@router.get("/me/users")
def creator_users(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  db: Session = Depends(get_db), user: models.User = Depends(require_creator)):
    query = db.query(models.UserCreatorReferral, models.User.email).join(models.User, models.User.id == models.UserCreatorReferral.user_id).join(
        models.CreatorCode, models.CreatorCode.code == models.UserCreatorReferral.original_code).filter(
        models.CreatorCode.creator_user_id == user.id, models.User.id != user.id).order_by(models.UserCreatorReferral.original_at.desc(), models.UserCreatorReferral.user_id)
    return paginated(query, page, page_size, lambda row: {"reference": payouts.reference(user.id, "user", row[0].user_id),
        "email": payouts.masked_email(row[1]), "code": row[0].original_code, "referred_at": row[0].original_at})


@router.get("/me/orders")
def creator_orders(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                   db: Session = Depends(get_db), user: models.User = Depends(require_creator)):
    query = db.query(models.Order, models.User.email).outerjoin(models.User, models.User.id == models.Order.user_id).filter(
        models.Order.creator_user_id == user.id, models.Order.user_id != user.id).order_by(models.Order.created_at.desc(), models.Order.id)
    def serialize(row):
        order, email = row
        earned = order.status in ("paid", "shipping", "completed") and not order.refund_status and order.figure_review_status != "rejected"
        eligible = earned and order.status == "completed" and order.paid_at and utc(order.paid_at) <= payouts.eligibility_cutoff()
        return {"reference": payouts.reference(user.id, "order", order.id), "customer_reference": payouts.reference(user.id, "user", order.user_id),
                "email": payouts.masked_email(email), "code": order.creator_code, "created_at": order.created_at,
                "product_amount_cents": round((order.price - order.discount_amount) * 100),
                "commission_cents": order.creator_commission_cents if earned else 0,
                "status": "eligible" if eligible else "pending" if earned else "excluded"}
    return paginated(query, page, page_size, serialize)


class PayoutRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000, strict=True)
    paypal_email: str = Field(max_length=127)
    request_key: UUID
    model_config = ConfigDict(extra="forbid")

    @field_validator("paypal_email")
    @classmethod
    def validate_email(cls, value): return payouts.email_address(value)


@router.post("/me/payouts")
@limiter.limit("10/minute", override_defaults=False)
def request_payout(request: Request, req: PayoutRequest, db: Session = Depends(get_db), user: models.User = Depends(require_creator)):
    payouts.lock_creator(db, user.id)
    previous = db.query(models.CreatorPayout).filter_by(creator_user_id=user.id, request_key=str(req.request_key)).first()
    if previous:
        if previous.amount_cents != req.amount_cents or previous.paypal_email != req.paypal_email:
            raise HTTPException(409, "creator_request_conflict")
        return payouts.payout_response(previous, db)
    if req.amount_cents > payouts.balance(db, user.id)["available_cents"]:
        raise HTTPException(409, "creator_insufficient_balance")
    payout = models.CreatorPayout(creator_user_id=user.id, amount_cents=req.amount_cents, paypal_email=req.paypal_email, request_key=str(req.request_key))
    db.add(payout); db.flush()
    payouts.event(db, payout, "requested", user.id)
    db.commit()
    return payouts.payout_response(payout, db)


@router.get("/me/payouts")
def own_payouts(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                db: Session = Depends(get_db), user: models.User = Depends(require_creator)):
    query = db.query(models.CreatorPayout).filter_by(creator_user_id=user.id).order_by(models.CreatorPayout.created_at.desc(), models.CreatorPayout.id)
    return paginated(query, page, page_size, lambda row: payouts.payout_response(row, db))


@router.get("/manage/payouts")
def manage_payouts(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), status: str | None = None,
                   db: Session = Depends(get_db), user: models.User = Depends(auth.get_current_admin)):
    query = db.query(models.CreatorPayout)
    if status: query = query.filter_by(status=status)
    result = paginated(query.order_by(models.CreatorPayout.created_at.desc(), models.CreatorPayout.id), page, page_size, lambda row: payouts.payout_response(row, db, admin=True))
    result["payouts_enabled"] = payouts.configured()
    return result


class PayoutReview(BaseModel):
    action: str = Field(pattern="^(approve|reject|sync)$")
    note: str = Field("", max_length=500)
    model_config = ConfigDict(extra="forbid")


@router.post("/manage/payouts/{payout_id}/review")
def review_payout(payout_id: str, req: PayoutReview, db: Session = Depends(get_db), admin: models.User = Depends(auth.get_current_admin)):
    payout = db.get(models.CreatorPayout, payout_id)
    if not payout: raise HTTPException(404, "creator_payout_not_found")
    payouts.lock_creator(db, payout.creator_user_id)
    payout = db.query(models.CreatorPayout).filter_by(id=payout_id).with_for_update().populate_existing().one()
    if req.action in ("approve", "reject"):
        if payout.status != "pending_review":
            if req.action == "approve" and payout.reviewed_at and payout.status != "rejected":
                return payouts.payout_response(payout, db, admin=True)
            raise HTTPException(409, "creator_payout_changed")
        if req.action == "approve":
            if not payouts.configured(): raise HTTPException(409, "creator_payouts_disabled")
            if payouts.balance(db, payout.creator_user_id)["adjustment_cents"]:
                raise HTTPException(409, "creator_insufficient_balance")
            payout.status = "processing"
        else:
            if not req.note.strip(): raise HTTPException(400, "creator_rejection_reason_required")
            payout.status = "rejected"
        payout.reviewed_by = admin.id; payout.reviewed_at = payouts.now(); payout.review_note = req.note.strip() or None
        payouts.event(db, payout, "approved" if req.action == "approve" else "rejected", admin.id)
        db.commit()
    else:
        if not payouts.configured(): raise HTTPException(409, "creator_payouts_disabled")
        db.rollback()
    if req.action != "reject": payouts.process_payout(db, payout_id, force=True)
    db.expire_all()
    return payouts.payout_response(db.get(models.CreatorPayout, payout_id), db, admin=True)
