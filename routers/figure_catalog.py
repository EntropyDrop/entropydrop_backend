"""Administrator settings for existing physical figure models."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

import auth
import models
from database import get_db
from figure_discounts import current_discount
from figure_shipping import current_shipping
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/api/figure/models", tags=["figure-admin"], dependencies=[Depends(auth.get_current_admin)])


class ModelSettingsResponse(BaseModel):
    id: str
    model_type: str
    stock: int
    price: float
    product_name: str | None


class ModelSettingsUpdate(BaseModel):
    stock: int = Field(ge=0, le=2147483647, strict=True)
    price: Decimal = Field(gt=0, le=Decimal("999999.99"), max_digits=8, decimal_places=2, allow_inf_nan=False)
    expected_stock: int = Field(strict=True)
    expected_price: float = Field(strict=True, allow_inf_nan=False)
    model_config = ConfigDict(extra="forbid")


def settings_response(product):
    return ModelSettingsResponse(
        id=product.id, model_type=product.model_type, stock=product.stock, price=product.price,
        product_name=(product.kit_specifications or {}).get("product_name"),
    )


@router.get("", response_model=list[ModelSettingsResponse])
def list_models(db: Session = Depends(get_db)):
    products = db.query(models.ModelSalesLimit).filter_by(order_type="print").order_by(models.ModelSalesLimit.model_type).all()
    return [settings_response(product) for product in products]


class GlobalDiscountUpdate(BaseModel):
    discount_bps: int = Field(ge=0, le=9900, strict=True)
    expected_version: int = Field(ge=0, strict=True)
    model_config = ConfigDict(extra="forbid")


@router.get("/discount")
def get_discount(db: Session = Depends(get_db)):
    return current_discount(db)


@router.patch("/discount")
def update_discount(req: GlobalDiscountUpdate, db: Session = Depends(get_db)):
    if req.expected_version == 0:
        db.add(models.FigureDiscount(id=1, discount_bps=req.discount_bps, version=1))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "figure_discount_changed")
    else:
        changed = db.query(models.FigureDiscount).filter_by(id=1, version=req.expected_version).update(
            {models.FigureDiscount.discount_bps: req.discount_bps, models.FigureDiscount.version: req.expected_version + 1},
            synchronize_session=False,
        )
        if changed != 1:
            db.rollback()
            raise HTTPException(409, "figure_discount_changed")
        db.commit()
    return current_discount(db)


class ShippingUpdate(BaseModel):
    shipping_fee: Decimal = Field(ge=0, le=Decimal("999999.99"), max_digits=8, decimal_places=2, allow_inf_nan=False)
    expected_version: int = Field(ge=0, strict=True)
    model_config = ConfigDict(extra="forbid")


@router.get("/shipping")
def get_shipping(db: Session = Depends(get_db)):
    return current_shipping(db)


@router.patch("/shipping")
def update_shipping(req: ShippingUpdate, db: Session = Depends(get_db)):
    if req.expected_version == 0:
        db.add(models.FigureShipping(id=1, shipping_fee=req.shipping_fee, version=1))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "figure_shipping_changed")
    else:
        changed = db.query(models.FigureShipping).filter_by(id=1, version=req.expected_version).update(
            {models.FigureShipping.shipping_fee: req.shipping_fee, models.FigureShipping.version: req.expected_version + 1},
            synchronize_session=False,
        )
        if changed != 1:
            db.rollback()
            raise HTTPException(409, "figure_shipping_changed")
        db.commit()
    return current_shipping(db)


@router.patch("/{model_id}", response_model=ModelSettingsResponse)
def update_model(model_id: str, req: ModelSettingsUpdate, db: Session = Depends(get_db)):
    query = db.query(models.ModelSalesLimit).filter_by(id=model_id, order_type="print")
    if not query.first():
        raise HTTPException(404, "Figure model not found")
    # A single conditional UPDATE serializes with checkout holds and refunds.
    # Never overwrite inventory or another admin's price changed since the read.
    changed = query.filter(
        models.ModelSalesLimit.stock == req.expected_stock,
        models.ModelSalesLimit.price == req.expected_price,
    ).update({models.ModelSalesLimit.stock: req.stock, models.ModelSalesLimit.price: float(req.price)}, synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "Stock or price has changed. Refresh model settings before trying again.")
    db.commit()
    return settings_response(query.populate_existing().one())
