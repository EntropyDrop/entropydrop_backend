"""Administrator-only human review and physical figure fulfillment."""
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session
import auth
import models
import schemas
from database import get_db
from figure_orders import notify_order, process_refund
from order_inventory import lock_order
from routers.order import order_response, presign_order_items

router = APIRouter(prefix="/api/figure/orders", tags=["figure-admin"], dependencies=[Depends(auth.get_current_admin)])


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str = Field("", max_length=2000)
    model_config = ConfigDict(extra="forbid")


class FulfillmentRequest(BaseModel):
    stage: Literal["printing", "shipping", "completed"]
    tracking_number: Optional[str] = Field(None, max_length=200)
    model_config = ConfigDict(extra="forbid")


def figure_order(db, order_id):
    order = lock_order(db, order_id)
    if order.order_type != "print":
        raise HTTPException(404, "Figure order not found")
    return order


def admin_response(db, order):
    data = order_response(db, order).model_dump(mode="json")
    data.update(figure_reviewed_by=order.figure_reviewed_by, refund_error=order.refund_error,
                paypal_refund_id=order.paypal_refund_id)
    source_map = {item.id: item for item in db.query(models.OrderItem).filter_by(order_id=order.id)}
    for item in data["items"]:
        saved = source_map[item['id']]
        item.update(refer_log_id=saved.refer_log_id, source_snapshot=saved.source_snapshot)
        # Explicitly marked as current metadata for older orders without snapshots.
        if not saved.source_snapshot and saved.refer_log_id:
            log = db.query(models.GenerationLog).filter_by(id=saved.refer_log_id).first()
            if log:
                item['source_current'] = dict(skin_id=log.id, name=log.name, publisher_id=log.user_id,
                                             parent_id=log.parent, license=log.license, public_license=log.public_license,
                                             is_public=log.is_public, license_version=log.license_version)
    return data


@router.get("")
def list_orders(
    stage: Literal["review", "production", "shipping", "refunds", "all"] = "review",
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50),
    order_id: Optional[str] = Query(None, max_length=16), db: Session = Depends(get_db),
):
    query = db.query(models.Order).filter(models.Order.order_type == "print")
    if order_id:
        query = query.filter(models.Order.id == order_id)
    if stage == "review":
        query = query.filter(models.Order.status == "paid", (models.Order.figure_review_status == "pending") | models.Order.figure_review_status.is_(None))
    elif stage == "production":
        query = query.filter(models.Order.status == "paid", models.Order.figure_review_status == "approved")
    elif stage == "shipping":
        query = query.filter(models.Order.status == "shipping")
    elif stage == "refunds":
        query = query.filter(models.Order.status.in_(["refund_pending", "refunded"]))
    total = query.count()
    orders = query.order_by(models.Order.created_at.asc(), models.Order.id.asc()).offset((page-1)*page_size).limit(page_size).all()
    return {"items": [admin_response(db, order) for order in orders], "total": total, "page": page, "total_pages": max(1, (total+page_size-1)//page_size)}


@router.post("/{order_id}/review")
def review_order(order_id: str, req: ReviewRequest, db: Session = Depends(get_db), admin: models.User = Depends(auth.get_current_admin)):
    order = figure_order(db, order_id)
    target = "approved" if req.decision == "approve" else "rejected"
    reason = req.reason.strip()
    if target == "rejected" and not reason:
        raise HTTPException(422, "A rejection reason is required")
    if order.figure_review_status == target:
        return admin_response(db, order)  # Duplicate clicks do not refund or notify twice.
    if order.status != "paid" or order.figure_review_status not in (None, "pending"):
        raise HTTPException(409, "Only paid orders awaiting review can be reviewed")
    order.figure_review_status = target
    order.figure_review_reason = reason or None
    order.figure_reviewed_by = admin.id
    order.figure_reviewed_at = datetime.now(timezone.utc)
    if target == "approved":
        order.goods_status = "preparing"
        notify_order(db, order, "approved")
    else:
        order.goods_status = "rejected"
        order.status = "refund_pending"
        order.refund_status = "queued"
        order.refund_requested_at = datetime.now(timezone.utc)
        notify_order(db, order, "rejected", reason)
    db.commit()  # Durable decision + mailbox BEFORE contacting the payment provider.
    if target == "rejected":
        order = process_refund(db, order.id)
    return admin_response(db, order)


@router.post("/{order_id}/refund/sync")
def sync_refund(order_id: str, db: Session = Depends(get_db)):
    order = figure_order(db, order_id)
    if order.figure_review_status != "rejected":
        raise HTTPException(409, "Only rejected orders have a refund to sync")
    order = process_refund(db, order.id)
    return admin_response(db, order)


@router.post("/{order_id}/fulfillment")
def update_fulfillment(order_id: str, req: FulfillmentRequest, db: Session = Depends(get_db)):
    order = figure_order(db, order_id)
    if order.figure_review_status != "approved" or order.status not in ("paid", "shipping", "completed"):
        raise HTTPException(409, "Human approval is required before production or shipment")
    if order.goods_status == req.stage:
        return admin_response(db, order)
    if req.stage == "printing" and order.status == "paid" and order.goods_status == "preparing":
        order.goods_status = "printing"
    elif req.stage == "shipping" and order.status == "paid" and order.goods_status == "printing":
        tracking = (req.tracking_number or "").strip()
        if not tracking:
            raise HTTPException(422, "A carrier and tracking number are required")
        order.tracking_number = tracking
        order.goods_status = order.status = "shipping"
        notify_order(db, order, "shipped", tracking)
    elif req.stage == "completed" and order.status == "shipping":
        order.goods_status = order.status = "completed"
        notify_order(db, order, "completed")
    else:
        raise HTTPException(409, "Invalid fulfillment transition")
    db.commit()
    return admin_response(db, order)


@router.get("/{order_id}/items/{item_id}/production-source", response_model=schemas.OrderItemResponse)
def production_source(order_id: str, item_id: str, db: Session = Depends(get_db)):
    order = figure_order(db, order_id)
    if order.figure_review_status != "approved" or order.status not in ("paid", "shipping", "completed"):
        raise HTTPException(409, "An approved paid order is required for production")
    item = db.query(models.OrderItem).filter_by(id=item_id, order_id=order.id).first()
    if not item:
        raise HTTPException(404, "Order item not found")
    if item.model_type != "Cute DIY Kit":
        raise HTTPException(409, "This model is not supported by the figure generator")
    if not item.skin_url or not item.skin_url.startswith(f"orders/{order.id}/{item.id}."):
        raise HTTPException(409, "The saved order skin is unavailable")
    if not item.sticker_snapshot or item.sticker_snapshot.get("missing_fields"):
        raise HTTPException(409, "Sticker snapshot is incomplete; review historical order data")
    # Sign the independent order copy afresh; never look up the original skin/user.
    return presign_order_items([item])[0]
