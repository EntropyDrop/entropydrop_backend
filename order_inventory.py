"""Inventory holds are durable before capture and consumed exactly once.

Callers lock the order before entering these helpers. An uncertain capture
keeps its hold until PayPal reconciliation; it must never expire blindly.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
import models
import schemas


def lock_order(db, order_id, user_id=None):
    query = db.query(models.Order).filter(models.Order.id == order_id)
    if user_id is not None:
        query = query.filter(models.Order.user_id == user_id)
    order = query.with_for_update().populate_existing().first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def quantities(db, order):
    return Counter(item.model_type for item in db.query(models.OrderItem).filter_by(order_id=order.id))


def snapshot_address(db, order):
    if order.address_snapshot is None and order.address_id:
        address = db.query(models.ShippingAddress).filter_by(id=order.address_id, user_id=order.user_id).with_for_update().first()
        if not address:
            raise HTTPException(status_code=409, detail="Shipping address is no longer available")
        order.address_snapshot = schemas.ShippingAddressResponse.model_validate(address).model_dump(mode="json")


def reserve_inventory(db, order):
    snapshot_address(db, order)
    if order.order_type != "print" or order.inventory_reserved:
        return
    # Atomic conditional updates also protect SQLite tests; PostgreSQL locks
    # the stock rows. A savepoint rolls back ALL holds if any model sells out.
    with db.begin_nested():
        for model_type, count in sorted(quantities(db, order).items()):
            changed = db.query(models.ModelSalesLimit).filter(
                models.ModelSalesLimit.model_type == model_type,
                models.ModelSalesLimit.order_type == "print",
                models.ModelSalesLimit.stock >= count,
            ).update({models.ModelSalesLimit.stock: models.ModelSalesLimit.stock - count}, synchronize_session=False)
            if changed != 1:
                raise HTTPException(status_code=409, detail=f"Insufficient stock for {model_type}")
        order.inventory_reserved = True
        order.inventory_reserved_until = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.flush()


def release_inventory(db, order):
    if order.capture_started:
        raise HTTPException(status_code=409, detail="Payment is being reconciled; order cannot be changed yet")
    if order.inventory_reserved:
        for model_type, count in sorted(quantities(db, order).items()):
            db.query(models.ModelSalesLimit).filter_by(model_type=model_type, order_type="print").update(
                {models.ModelSalesLimit.stock: models.ModelSalesLimit.stock + count}, synchronize_session=False
            )
        order.inventory_reserved = False
        order.inventory_reserved_until = None


def expire_inventory_holds(db):
    now = datetime.now(timezone.utc)
    ids = [row.id for row in db.query(models.Order.id).filter(
        models.Order.status == "pending_payment",
        models.Order.inventory_reserved == True,
        models.Order.capture_started == False,
        models.Order.inventory_reserved_until <= now,
    )]
    db.commit()
    for order_id in ids:
        order = lock_order(db, order_id)
        expires = order.inventory_reserved_until
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if order.status == "pending_payment" and not order.capture_started and expires and expires <= now:
            release_inventory(db, order)
            # The old PayPal voucher cannot outlive its uncaptured hold. The
            # next checkout creates a fresh voucher and rechecks availability.
            order.paypal_order_id = None
        db.commit()
