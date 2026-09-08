"""Entitlements use PayPal's verified payment time, never the activation time.

PayPal billing_info.last_payment is the last successful payment; see
https://developer.paypal.com/api/subscriptions/v1/definitions/subscription/
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException


def paid_cycle(subscription):
    billing = subscription.get("billing_info") or {}
    payment = billing.get("last_payment") or {}
    amount = payment.get("amount") or {}
    try:
        paid_at = datetime.fromisoformat(payment["time"].replace("Z", "+00:00"))
        if paid_at.tzinfo is None:
            raise ValueError("Missing payment timezone")
        paid_at = paid_at.astimezone(timezone.utc)
        value = Decimal(amount["value"])
        if not value.is_finite() or value <= 0 or amount.get("currency_code") != "USD":
            raise ValueError("Payment must be positive USD")
        if paid_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Future payment")
        next_time = billing.get("next_billing_time")
        end = datetime.fromisoformat(next_time.replace("Z", "+00:00")) if next_time else paid_at + timedelta(days=31)
        if end.tzinfo is None or end <= paid_at:
            raise ValueError("Invalid billing period")
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise HTTPException(status_code=409, detail="Subscription payment is not yet verified; retry after payment completes")
    return paid_at, end + timedelta(days=3)
