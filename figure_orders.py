"""Figure fulfillment and refund state. Every mutation holds the order row lock."""
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
import models
from order_inventory import lock_order, quantities
from payment_utils import get_paypal_order_api, get_paypal_refund_api, refund_paypal_capture_api
from routers.order import _money, _validate_paypal_id, _validate_paypal_payload_for_order, _purchase_unit


def notify_order(db, order, event, message=None):
    key = f"figure:{order.id}:{event}"
    if not db.query(models.ForumNotification.id).filter_by(event_key=key).first():
        db.add(models.ForumNotification(
            user_id=order.user_id, type=f"figure_{event}", order_id=order.id,
            message=message, event_key=key, is_read=False,
        ))


def _manual(order, message):
    order.refund_status = "manual_review"
    order.refund_error = message


def _apply_refund(db, order, refund):
    refund_id = _validate_paypal_id(refund.get("id"), "PayPal refund ID")
    amount = refund.get("amount") or {}
    if amount.get("currency_code") != "USD" or _money(amount.get("value")) != _money(order.total_price):
        raise ValueError("Refund amount or currency does not match the order")
    if order.paypal_refund_id and order.paypal_refund_id != refund_id:
        raise ValueError("Refund ID mismatch")
    capture_links = [link.get("href", "").rstrip('/').split('/')[-1] for link in refund.get("links", []) if link.get("rel") == "up"]
    if capture_links and order.paypal_capture_id not in capture_links:
        raise ValueError("Refund capture does not match the order")
    status = refund.get("status")
    if status not in ("COMPLETED", "PENDING", "FAILED", "CANCELLED"):
        raise ValueError("Unknown PayPal refund status")
    order.paypal_refund_id = refund_id
    order.refund_error = None
    if status == "COMPLETED":
        order.status = "refunded"
        order.refund_status = "completed"
        if order.inventory_consumed:
            for model_type, count in sorted(quantities(db, order).items()):
                db.query(models.ModelSalesLimit).filter_by(model_type=model_type, order_type="print").update(
                    {models.ModelSalesLimit.stock: models.ModelSalesLimit.stock + count}, synchronize_session=False)
            order.inventory_consumed = False
        notify_order(db, order, "refunded", order.figure_review_reason)
    elif status == "PENDING":
        order.refund_status = "pending"
    else:
        order.refund_status = "failed"
        order.refund_error = f"PayPal refund {status.lower()}; resolve with PayPal, then sync again."
        notify_order(db, order, "refund_delayed", order.figure_review_reason)


def process_refund(db, order_id):
    order = lock_order(db, order_id)
    if order.order_type != "print" or order.figure_review_status != "rejected" or order.status != "refund_pending":
        return order
    now = datetime.now(timezone.utc)
    order.refund_checked_at = now
    try:
        if order.paypal_refund_id:
            _apply_refund(db, order, get_paypal_refund_api(_validate_paypal_id(order.paypal_refund_id)))
        else:
            payload = get_paypal_order_api(_validate_paypal_id(order.paypal_order_id))
            _validate_paypal_payload_for_order(order, payload)
            if len(payload.get("purchase_units", [])) != 1:
                raise ValueError("Multiple purchase units require manual reconciliation")
            payments = _purchase_unit(payload).get("payments", {})
            captures = payments.get("captures", [])
            if len(captures) != 1:
                raise ValueError("Expected exactly one capture for a full order refund")
            capture = captures[0]
            capture_id = _validate_paypal_id(capture.get("id"), "PayPal capture ID")
            if order.paypal_capture_id and order.paypal_capture_id != capture_id:
                raise ValueError("Capture ID mismatch")
            order.paypal_capture_id = capture_id
            refunds = payments.get("refunds", [])
            if refunds:
                # Adopt a lost response or an out-of-band FULL refund. Partial or
                # multiple refunds need manual reconciliation, never another POST.
                if len(refunds) != 1:
                    raise ValueError("Multiple refunds require manual reconciliation")
                _apply_refund(db, order, get_paypal_refund_api(_validate_paypal_id(refunds[0].get("id"))))
            elif capture.get("status") != "COMPLETED":
                raise ValueError("Capture is not fully refundable; check PayPal before proceeding")
            else:
                started = order.refund_requested_at
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if not started or now - started > timedelta(hours=6):
                    _manual(order, "Refund result is unconfirmed. Check PayPal; sync only until the existing refund is located.")
                else:
                    request_id = str(uuid5(NAMESPACE_URL, f"entropydrop:figure-refund:{order.id}"))
                    refund = refund_paypal_capture_api(capture_id, str(_money(order.total_price)), request_id)
                    _apply_refund(db, order, refund)
    except (ValueError, HTTPException) as exc:
        _manual(order, str(getattr(exc, 'detail', exc))[:500])
    except Exception:
        # Network/PayPal failure has an UNKNOWN outcome. Retain the same key and
        # reconcile before retrying, including after a crash following the POST.
        order.refund_error = "PayPal confirmation unavailable. Sync to check the existing refund."
        if order.refund_status not in ("failed", "manual_review"):
            order.refund_status = "pending"
    db.commit()
    return order


def reconcile_figure_refunds():
    from database import SessionLocal
    with SessionLocal() as db:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        ids = [row.id for row in db.query(models.Order.id).filter(
            models.Order.order_type == "print", models.Order.status == "refund_pending",
            models.Order.refund_status.in_(["queued", "pending"]),
            (models.Order.refund_checked_at.is_(None)) | (models.Order.refund_checked_at < cutoff),
        ).order_by(models.Order.refund_requested_at).limit(20)]
        db.commit()
        for order_id in ids:
            try:
                process_refund(db, order_id)
            except Exception:
                db.rollback()
