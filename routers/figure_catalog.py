"""Administrator settings for existing physical figure models."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

import auth
import models
from database import get_db

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
