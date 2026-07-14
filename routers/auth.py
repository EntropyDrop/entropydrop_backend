from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
import models
import schemas
import auth
from datetime import datetime, date
import backend_utils
from backend_utils import is_text_to_skin_enabled, is_image_to_skin_enabled, is_image_edit_to_skin_enabled

router = APIRouter(tags=["auth"])



@router.post("/api/auth/google", response_model=schemas.TokenResponse)
async def google_login(req: schemas.GoogleAuthRequest, db: Session = Depends(get_db)):
    # Verify Google token...
    id_info = auth.verify_google_token(req.token)
    email = id_info.get("email")
    google_id = id_info.get("sub")
    name = id_info.get("name")
    picture = id_info.get("picture")
    email_verified = id_info.get("email_verified")
    
    if not email:
        raise HTTPException(status_code=400, detail="Google token does not contain email")

    if email_verified is not True and str(email_verified).lower() != "true":
        raise HTTPException(status_code=400, detail="Google email is not verified")

    email = email.strip().lower()
    if not google_id:
        raise HTTPException(status_code=400, detail="Google token does not contain subject")
        
    user = db.query(models.User).filter(func.lower(models.User.email) == email).first()
    if not user:
        user = models.User(email=email, username=name, picture=picture, google_id=google_id, priority_points=0, terms_agreed=False)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        if user.google_id and user.google_id != google_id:
            raise HTTPException(status_code=403, detail="Google account does not match this user")
        user.google_id = google_id
        user.username = user.username or name
        user.picture = picture or user.picture
        db.commit()
        db.refresh(user)
        
    access_token = auth.create_access_token(data={"sub": user.id})
    import backend_utils
    backend_utils.award_daily_login_credits(db, user)
    user_res = {
        "id": user.id,
        "email": user.email,
        "username": user.username,
        "picture": user.picture,
        "google_id": user.google_id,
        "is_pro": user.is_pro,
        "is_admin": user.is_admin,
        "pro_expires_at": user.pro_expires_at,
        "terms_agreed": user.terms_agreed,
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
        "pro_level": user.pro_level,
        "credits": user.credits,
        "paypal_subscription_id": user.paypal_subscription_id,
        "paypal_subscription_status": user.paypal_subscription_status,
        "minecraft_skin_url": user.minecraft_skin_url
    }
    return {"access_token": access_token, "token_type": "bearer", "user": user_res}

@router.get("/api/users/me", response_model=schemas.UserResponse)
async def get_my_profile(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    user_res = {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "picture": current_user.picture,
        "google_id": current_user.google_id,
        "is_pro": current_user.is_pro,
        "is_admin": current_user.is_admin,
        "pro_expires_at": current_user.pro_expires_at,
        "terms_agreed": current_user.terms_agreed,
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
        "pro_level": current_user.pro_level,
        "credits": current_user.credits,
        "paypal_subscription_id": current_user.paypal_subscription_id,
        "paypal_subscription_status": current_user.paypal_subscription_status,
        "minecraft_skin_url": current_user.minecraft_skin_url
    }
    return user_res

@router.post("/api/users/agree_terms", response_model=schemas.UserResponse)
async def agree_terms(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    current_user.terms_agreed = True
    db.commit()
    db.refresh(current_user)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "picture": current_user.picture,
        "google_id": current_user.google_id,
        "is_pro": current_user.is_pro,
        "is_admin": current_user.is_admin,
        "pro_expires_at": current_user.pro_expires_at,
        "terms_agreed": current_user.terms_agreed,
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
        "pro_level": current_user.pro_level,
        "credits": current_user.credits,
        "paypal_subscription_id": current_user.paypal_subscription_id,
        "paypal_subscription_status": current_user.paypal_subscription_status,
        "minecraft_skin_url": current_user.minecraft_skin_url
    }

@router.post("/api/users/me/cancel_subscription")
async def cancel_subscription(db: Session = Depends(get_db), current_user: models.User = Depends(auth.get_current_user)):
    from payment_utils import cancel_paypal_subscription_api
    
    sub_id = current_user.paypal_subscription_id
    if not sub_id or current_user.paypal_subscription_status != "ACTIVE":
        raise HTTPException(status_code=400, detail="No active auto-renewing subscription found")
        
    try:
        cancel_paypal_subscription_api(sub_id, reason="User cancelled via dashboard")
        current_user.paypal_subscription_status = "CANCELLED"
        db.commit()
        db.refresh(current_user)
        return {"status": "success", "message": "Subscription cancelled successfully"}
    except Exception as e:
        print(f"Failed to cancel subscription: {e}")
        raise HTTPException(status_code=500, detail="Failed to cancel subscription with PayPal")

@router.post("/api/users/me/username", response_model=schemas.UserResponse)
async def update_username(
    req: schemas.UpdateUsernameRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    current_user.username = req.username
    db.commit()
    db.refresh(current_user)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "picture": current_user.picture,
        "google_id": current_user.google_id,
        "is_pro": current_user.is_pro,
        "is_admin": current_user.is_admin,
        "pro_expires_at": current_user.pro_expires_at,
        "terms_agreed": current_user.terms_agreed,
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
        "pro_level": current_user.pro_level,
        "credits": current_user.credits,
        "paypal_subscription_id": current_user.paypal_subscription_id,
        "paypal_subscription_status": current_user.paypal_subscription_status,
        "minecraft_skin_url": current_user.minecraft_skin_url
    }

@router.post("/api/users/me/minecraft_skin", response_model=schemas.UserResponse)
async def update_minecraft_skin(
    req: schemas.UpdateMinecraftSkinRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    current_user.minecraft_skin_url = req.minecraft_skin_url
    db.commit()
    db.refresh(current_user)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "username": current_user.username,
        "picture": current_user.picture,
        "google_id": current_user.google_id,
        "is_pro": current_user.is_pro,
        "is_admin": current_user.is_admin,
        "pro_expires_at": current_user.pro_expires_at,
        "terms_agreed": current_user.terms_agreed,
        "text_to_skin_enabled": is_text_to_skin_enabled(),
        "image_to_skin_enabled": is_image_to_skin_enabled(),
        "image_edit_to_skin_enabled": is_image_edit_to_skin_enabled(),
        "pro_level": current_user.pro_level,
        "credits": current_user.credits,
        "paypal_subscription_id": current_user.paypal_subscription_id,
        "paypal_subscription_status": current_user.paypal_subscription_status,
        "minecraft_skin_url": current_user.minecraft_skin_url
    }

@router.get("/api/users/me/credits/history")
async def get_my_credit_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    skip = (page - 1) * page_size
    query = db.query(models.CreditLog).filter(
        models.CreditLog.user_id == current_user.id
    )
    total = query.count()
    logs = query.order_by(models.CreditLog.created_at.desc()).offset(skip).limit(page_size).all()
    
    results = []
    for log in logs:
        results.append({
            "id": log.id,
            "amount": log.amount,
            "action": log.action,
            "source": log.source,
            "timestamp": log.created_at.replace(tzinfo=None).isoformat() + "Z"
        })
        
    return backend_utils.paginate_response(results, total, page, page_size)
