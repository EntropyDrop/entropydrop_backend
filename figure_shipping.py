"""Configurable flat shipping for CUTE kit orders."""
from decimal import Decimal

from fastapi import HTTPException
import models

DEFAULT_SHIPPING_FEE = Decimal("12.90")


def current_shipping(db):
    saved = db.query(models.FigureShipping).filter_by(id=1).first()
    return {"shipping_fee": float(saved.shipping_fee), "version": saved.version} if saved else {
        "shipping_fee": float(DEFAULT_SHIPPING_FEE), "version": 0,
    }


def model_shipping_fee(db, order_type, model_type, expected=None):
    fee = current_shipping(db)["shipping_fee"] if order_type == "print" and model_type == "Cute DIY Kit" else 0.0
    if expected is not None and expected != Decimal(str(fee)):
        raise HTTPException(409, "figure_shipping_changed")
    return fee
