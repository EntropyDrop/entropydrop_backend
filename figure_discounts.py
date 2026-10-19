"""Global figure promotions, independent of creator attribution."""
from fastapi import HTTPException
import models
from creator_referrals import money


def current_discount(db, expected_version=None):
    saved = db.query(models.FigureDiscount).filter_by(id=1).first()
    offer = {"discount_bps": saved.discount_bps, "version": saved.version} if saved else {"discount_bps": 0, "version": 0}
    if expected_version is not None and expected_version != offer["version"]:
        raise HTTPException(409, "figure_discount_changed")
    return offer


def discount_amount(subtotal, bps):
    return money(money(subtotal) * bps / 10000)
