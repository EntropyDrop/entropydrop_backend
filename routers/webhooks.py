import json
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

import models
from config import settings
from database import get_db
from rate_limit import limiter


router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _paypal_plan_to_pro_level(plan_id: str):
    if plan_id and plan_id == settings.PAYPAL_PRO_MAX_PLAN_ID:
        return "pro-max"
    if plan_id and plan_id == settings.PAYPAL_PRO_PLUS_PLAN_ID:
        return "pro-plus"
    return None


def _resolve_subscription_user(db, sub_id: str, subscription: dict):
    """Resolve the owner when a PayPal webhook beats browser activation."""
    user = (
        db.query(models.User)
        .filter(models.User.paypal_subscription_id == sub_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if user:
        return user

    custom_id = subscription.get("custom_id")
    if not isinstance(custom_id, str) or not custom_id:
        raise HTTPException(
            status_code=409,
            detail="Subscription owner is not linked yet; retry webhook",
        )

    user = (
        db.query(models.User)
        .filter(models.User.id == custom_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=409,
            detail="Subscription owner does not exist yet; retry webhook",
        )

    # custom_id comes from a fresh authenticated lookup of this exact PayPal
    # subscription, so it can safely replace a stale subscription binding.
    user.paypal_subscription_id = sub_id
    return user


def _completed_sale_amount(resource: dict, subscription: dict) -> float:
    sale_amount = resource.get("amount") or {}
    last_payment = ((subscription.get("billing_info") or {}).get("last_payment") or {})
    paid_amount = last_payment.get("amount") or {}
    try:
        value = Decimal(sale_amount["total"])
        verified_value = Decimal(paid_amount["value"])
        if (
            not value.is_finite()
            or value <= 0
            or sale_amount.get("currency") != "USD"
            or paid_amount.get("currency_code") != "USD"
            or value != verified_value
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise HTTPException(status_code=400, detail="Invalid subscription payment amount")
    return float(value)


@router.post("/paypal")
@limiter.exempt
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    return await run_in_threadpool(_process_paypal_webhook, body, dict(request.headers), db)


def _paypal_subscription(sub_id: str):
    from payment_utils import get_paypal_subscription_api

    try:
        return get_paypal_subscription_api(sub_id)
    except Exception:
        raise HTTPException(status_code=503, detail="Failed to fetch subscription")


def _process_paypal_webhook(body, headers, db):
    from payment_utils import verify_paypal_webhook_signature

    if not settings.PAYPAL_WEBHOOK_ID:
        raise HTTPException(status_code=400, detail="PayPal webhook ID is not configured")

    if not verify_paypal_webhook_signature(headers, body, settings.PAYPAL_WEBHOOK_ID):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(body)
    event_id = payload.get("id")
    event_type = payload.get("event_type")
    resource = payload.get("resource", {})

    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        sub_id = resource.get("id")
        if not sub_id:
            raise HTTPException(status_code=400, detail="Subscription ID is missing")
        user = (
            db.query(models.User)
            .filter(models.User.paypal_subscription_id == sub_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not user:
            sub_data = _paypal_subscription(sub_id)
            if not _paypal_plan_to_pro_level(sub_data.get("plan_id")):
                raise HTTPException(status_code=400, detail="Subscription plan is not supported")
            user = _resolve_subscription_user(db, sub_id, sub_data)
        user.paypal_subscription_status = "ACTIVE"
        db.commit()
        print(f"Linked activated subscription {sub_id} to user {user.id} from event {event_id}")

    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        sub_id = resource.get("id")
        if not sub_id:
            raise HTTPException(status_code=400, detail="Subscription ID is missing")
        user = (
            db.query(models.User)
            .filter(models.User.paypal_subscription_id == sub_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if not user:
            user = _resolve_subscription_user(db, sub_id, _paypal_subscription(sub_id))
        user.paypal_subscription_status = "CANCELLED"
        db.commit()

    elif event_type == "PAYMENT.SALE.COMPLETED":
        sub_id = resource.get("billing_agreement_id")
        sale_id = resource.get("id")
        if not sub_id or not sale_id:
            raise HTTPException(
                status_code=400,
                detail="Subscription payment identifiers are missing",
            )

        sub_data = _paypal_subscription(sub_id)
        pro_level = _paypal_plan_to_pro_level(sub_data.get("plan_id"))
        if not pro_level:
            raise HTTPException(status_code=400, detail="Subscription plan is not supported")

        amount_total = _completed_sale_amount(resource, sub_data)
        from subscription_billing import paid_cycle

        paid_at, expires_at = paid_cycle(sub_data)
        user = _resolve_subscription_user(db, sub_id, sub_data)

        # The user row lock serializes duplicate delivery across API tasks.
        existing_order = (
            db.query(models.Order)
            .filter(models.Order.paypal_order_id == sale_id)
            .first()
        )
        if existing_order:
            db.rollback()
            return {"status": "success"}

        user.pro_expires_at = expires_at
        user.paypal_subscription_status = "ACTIVE"
        user.pro_level = pro_level

        order = models.Order(
            user_id=user.id,
            order_type="subscription",
            status="paid",
            price=amount_total,
            shipping_fee=0.0,
            total_price=amount_total,
            paid_at=paid_at,
            paypal_order_id=sale_id,
            goods_status=None,
            address_id=None,
        )
        db.add(order)
        db.flush()
        db.add(models.OrderItem(
            order_id=order.id,
            model_type=pro_level,
            price=amount_total,
            skin_url=None,
            refer_log_id=None,
        ))

        import backend_utils

        backend_utils.award_subscription_credits(
            db,
            user,
            pro_level,
            sub_id,
            is_webhook=True,
            paid_at=paid_at,
        )
        db.commit()
        print(f"Processed subscription sale {sale_id} for user {user.id} from event {event_id}")

    return {"status": "success"}
