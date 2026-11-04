import json
import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import NoResultFound
from starlette.concurrency import run_in_threadpool

import models
import generation_priority
import subscription_lifecycle as subscriptions
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


def _subscription_context(db, sub_id):
    if not isinstance(sub_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", sub_id):
        raise HTTPException(status_code=400, detail="Invalid subscription ID")
    contract = db.query(models.PaypalSubscription).filter_by(paypal_id=sub_id).first()
    user_id = contract.user_id if contract else db.query(models.User.id).filter_by(paypal_subscription_id=sub_id).scalar()
    if not user_id:
        user_id = _paypal_subscription(sub_id).get("custom_id")
    try:
        user = subscriptions.lock_user(db, user_id) if user_id else None
    except NoResultFound:
        user = None
    if user is None:
        raise HTTPException(status_code=409, detail="Subscription owner is not linked yet; retry webhook")
    # Fetch AFTER the owner lock. A concurrent cancel must not be overwritten by
    # an ACTIVE snapshot fetched before that cancellation completed.
    data = _paypal_subscription(sub_id)
    if data.get("custom_id") and data["custom_id"] != user.id:
        raise HTTPException(status_code=403, detail="Subscription owner mismatch")
    return user, data


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

    if isinstance(event_type, str) and event_type.startswith("PAYMENT.PAYOUTS-"):
        # Signed events only schedule reconciliation. Never trust a webhook's
        # amount/status as proof of payment; the worker fetches PayPal directly.
        from creator_payouts import now
        batch_id = resource.get("payout_batch_id") or resource.get("batch_header", {}).get("payout_batch_id")
        item_id = resource.get("payout_item_id")
        query = db.query(models.CreatorPayout)
        if batch_id: payout = query.filter_by(paypal_batch_id=batch_id).first()
        elif item_id: payout = query.filter_by(paypal_item_id=item_id).first()
        else: payout = None
        if payout:
            payout.next_check_at = now()
            db.commit()
        return {"status": "success"}

    if event_type in {
        "BILLING.SUBSCRIPTION.CREATED", "BILLING.SUBSCRIPTION.ACTIVATED",
        "BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED",
        "BILLING.SUBSCRIPTION.EXPIRED", "BILLING.SUBSCRIPTION.UPDATED",
    }:
        sub_id = resource.get("id")
        user, sub_data = _subscription_context(db, sub_id)
        if event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"}:
            # A signed terminal event is authoritative even if GET lags behind.
            sub_data = {**sub_data, "status": event_type.rsplit(".", 1)[-1],
                        "status_update_time": resource.get("status_update_time") or payload.get("create_time")}
        elif not _paypal_plan_to_pro_level(sub_data.get("plan_id")):
            raise HTTPException(status_code=400, detail="Subscription plan is not supported")
        contract = subscriptions.remember(db, user, sub_id, sub_data)
        subscriptions.select_contract(db, user, contract)
        db.commit()

    elif event_type == "PAYMENT.SALE.COMPLETED":
        sub_id = resource.get("billing_agreement_id")
        sale_id = resource.get("id")
        if not sub_id or not sale_id:
            raise HTTPException(
                status_code=400,
                detail="Subscription payment identifiers are missing",
            )

        user, sub_data = _subscription_context(db, sub_id)
        pro_level = _paypal_plan_to_pro_level(sub_data.get("plan_id"))
        if not pro_level:
            raise HTTPException(status_code=400, detail="Subscription plan is not supported")

        amount_total = _completed_sale_amount(resource, sub_data)
        from subscription_billing import paid_cycle

        paid_at, expires_at = paid_cycle(sub_data)
        contract = subscriptions.remember(db, user, sub_id, sub_data)
        subscriptions.select_contract(db, user, contract)

        # The user row lock serializes duplicate delivery across API tasks.
        existing_order = (
            db.query(models.Order)
            .filter(models.Order.paypal_order_id == sale_id)
            .first()
        )
        if existing_order:
            db.commit()
            generation_priority.sync_after_payment(db, user.id)
            return {"status": "success"}

        # Historical payments still belong in the ledger, but must not shorten
        # newer paid entitlements or change the selected subscription's plan.
        if not user.pro_expires_at or expires_at > subscriptions.utc(user.pro_expires_at):
            user.pro_expires_at = expires_at
        if user.paypal_subscription_id == sub_id or user.pro_level == "free":
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
        generation_priority.grant_pro_priority(db, user)
        db.commit()
        generation_priority.sync_after_payment(db, user.id)
        print(f"Processed subscription sale {sale_id} for user {user.id} from event {event_id}")

    return {"status": "success"}
