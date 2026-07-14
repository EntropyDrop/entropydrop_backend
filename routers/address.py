from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from database import get_db
import models
import schemas
import auth

router = APIRouter(prefix="/api/addresses", tags=["address"])

@router.get("", response_model=List[schemas.ShippingAddressResponse])
async def get_addresses(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Get all shipping addresses for the user"""
    addresses = db.query(models.ShippingAddress).filter(
        models.ShippingAddress.user_id == current_user.id
    ).order_by(models.ShippingAddress.is_default.desc(), models.ShippingAddress.created_at.desc()).all()
    return addresses

@router.post("", response_model=schemas.ShippingAddressResponse)
async def create_address(
    req: schemas.ShippingAddressCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Create new shipping address"""
    # Limit to 10 addresses
    count = db.query(models.ShippingAddress).filter(
        models.ShippingAddress.user_id == current_user.id
    ).count()
    if count >= 10:
        raise HTTPException(status_code=400, detail="Maximum of 10 shipping addresses can be stored")

    # If set as default, unset other default addresses first
    if req.is_default:
        db.query(models.ShippingAddress).filter(
            models.ShippingAddress.user_id == current_user.id,
            models.ShippingAddress.is_default == True
        ).update({"is_default": False})

    # If it's the first address and no default set, automatically set as default (Optional)
    
    address = models.ShippingAddress(
        user_id=current_user.id,
        country=req.country,
        phone=req.phone,
        zip_code=req.zip_code,
        state=req.state,
        city=req.city,
        detail_address=req.detail_address,
        is_default=req.is_default
    )
    db.add(address)
    db.commit()
    db.refresh(address)
    return address

@router.put("/{id}", response_model=schemas.ShippingAddressResponse)
async def update_address(
    id: str,
    req: schemas.ShippingAddressUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Update shipping address"""
    address = db.query(models.ShippingAddress).filter(
        models.ShippingAddress.id == id,
        models.ShippingAddress.user_id == current_user.id
    ).first()
    
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")

    # If updated to default, unset other default addresses first
    if req.is_default:
        db.query(models.ShippingAddress).filter(
            models.ShippingAddress.user_id == current_user.id,
            models.ShippingAddress.id != id,
            models.ShippingAddress.is_default == True
        ).update({"is_default": False})

    # Update fields
    update_data = req.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(address, key, value)

    db.commit()
    db.refresh(address)
    return address

@router.delete("/{id}")
async def delete_address(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Delete shipping address"""
    address = db.query(models.ShippingAddress).filter(
        models.ShippingAddress.id == id,
        models.ShippingAddress.user_id == current_user.id
    ).first()
    
    if not address:
        raise HTTPException(status_code=404, detail="Address not found")

    db.delete(address)
    db.commit()
    return {"message": "Address deleted"}
