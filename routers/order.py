from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from decimal import Decimal, InvalidOperation
import re
from database import get_db
import models
import schemas
import auth
import uuid
import datetime
from datetime import timezone
from config import settings
from s3_utils import s3_client
from payment_utils import (
    create_paypal_order_api,
    capture_paypal_order_api,
    get_paypal_order_api,
    get_paypal_subscription_api,
)
import backend_utils

PAYPAL_CURRENCY_CODE = "USD"


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid PayPal amount")


def _purchase_unit(payload: dict) -> dict:
    units = payload.get("purchase_units") or []
    if not units:
        raise HTTPException(status_code=400, detail="PayPal order is missing purchase units")
    return units[0]


def _capture_from_unit(unit: dict) -> dict | None:
    captures = (
        unit.get("payments", {})
        .get("captures", [])
    )
    return captures[0] if captures else None


def _payload_amount(unit: dict) -> dict:
    capture = _capture_from_unit(unit)
    if capture and capture.get("amount"):
        return capture["amount"]
    if unit.get("amount"):
        return unit["amount"]
    raise HTTPException(status_code=400, detail="PayPal order is missing amount")


def _validate_paypal_payload_for_order(order: models.Order, payload: dict) -> None:
    unit = _purchase_unit(payload)
    custom_id = unit.get("custom_id")
    if custom_id != order.id:
        raise HTTPException(status_code=400, detail="PayPal order binding mismatch")

    amount = _payload_amount(unit)
    currency = amount.get("currency_code")
    if currency != PAYPAL_CURRENCY_CODE:
        raise HTTPException(status_code=400, detail="PayPal currency mismatch")

    if _money(amount.get("value")) != _money(order.total_price):
        raise HTTPException(status_code=400, detail="PayPal amount mismatch")


_PAYPAL_ID_RE = re.compile(r"^[A-Za-z0-9\-_]+$")


def _validate_paypal_id(value: str, label: str = "PayPal ID") -> str:
    """Ensure a PayPal identifier contains only safe characters."""
    if not value or not _PAYPAL_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}")
    return value


def _paypal_payload_is_completed(payload: dict) -> bool:
    if payload.get("status") == "COMPLETED":
        return True
    try:
        capture = _capture_from_unit(_purchase_unit(payload))
        return bool(capture and capture.get("status") == "COMPLETED")
    except HTTPException:
        return False


def confirm_paypal_payment(order: models.Order, paypal_order_id: Optional[str]) -> dict:
    if not paypal_order_id:
        raise HTTPException(status_code=400, detail="PayPal order ID is required")
    _validate_paypal_id(paypal_order_id, "PayPal order ID")
    if not order.paypal_order_id:
        raise HTTPException(status_code=400, detail="PayPal order has not been created for this order")
    if paypal_order_id != order.paypal_order_id:
        raise HTTPException(status_code=400, detail="Payment voucher mismatch")

    paypal_order = get_paypal_order_api(paypal_order_id)
    _validate_paypal_payload_for_order(order, paypal_order)
    status = paypal_order.get("status")

    if status == "COMPLETED":
        return paypal_order
    if status == "APPROVED":
        capture_data = capture_paypal_order_api(paypal_order_id)
        _validate_paypal_payload_for_order(order, capture_data)
        if not _paypal_payload_is_completed(capture_data):
            raise HTTPException(status_code=400, detail="PayPal payment not completed")
        return capture_data

    raise HTTPException(status_code=400, detail=f"Incorrect PayPal order status: {status}")


def _paypal_plan_to_pro_level(plan_id: str) -> Optional[str]:
    if plan_id and plan_id == settings.PAYPAL_PRO_MAX_PLAN_ID:
        return "pro-max"
    if plan_id and plan_id == settings.PAYPAL_PRO_PLUS_PLAN_ID:
        return "pro-plus"
    return None


def _subscription_subscriber_email(subscription: dict) -> Optional[str]:
    subscriber = subscription.get("subscriber") or {}
    email = subscriber.get("email_address")
    return email.lower() if isinstance(email, str) and email else None


def _subscription_custom_id(subscription: dict) -> Optional[str]:
    custom_id = subscription.get("custom_id")
    return custom_id.strip() if isinstance(custom_id, str) and custom_id.strip() else None


def clone_skin_for_order(log_entry, order_id, item_id):
    if not log_entry or not log_entry.result:
        return None
    source_bucket = settings.AWS_BUCKET_NAME if log_entry.is_public else settings.AWS_PRIVATE_BUCKET_NAME
    source_key = log_entry.result
    # result typically is subpath or full file path key
    destination_key = f"orders/{order_id}/{item_id}.png"
    
    try:
        s3_client.copy_object(
            Bucket=settings.AWS_PRIVATE_BUCKET_NAME,
            CopySource={'Bucket': source_bucket, 'Key': source_key},
            Key=destination_key
        )
        return destination_key
    except Exception as e:
        print(f"Failed to copy skin for order: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to save custom file")

def presign_order_items(items):
    from s3_utils import generate_presigned_url_get
    for item in items:
        if getattr(item, 'skin_url', None):
            item.skin_url = generate_presigned_url_get(item.skin_url, bucket=settings.AWS_PRIVATE_BUCKET_NAME)
    return items

def _activate_order_benefits(order, db: Session, current_user):
    if order.order_type == "subscription":
        months = 1 # All subscriptions are now monthly
        item = db.query(models.OrderItem).filter(models.OrderItem.order_id == order.id).first()
        
        now = datetime.datetime.now(datetime.timezone.utc)
             
        if current_user.pro_expires_at:
             expires_at = current_user.pro_expires_at
             if expires_at.tzinfo is None:
                 expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
             
             if expires_at > now:
                  current_user.pro_expires_at = expires_at + datetime.timedelta(days=31 * months)
             else:
                  current_user.pro_expires_at = now + datetime.timedelta(days=31 * months)
        else:
             current_user.pro_expires_at = now + datetime.timedelta(days=31 * months)
             
        # Update pro_level
        if item and item.model_type == "pro-max":
            current_user.pro_level = "pro-max"
        else:
            current_user.pro_level = "pro-plus"
             
        #current_user.priority_points += 10
    elif order.order_type == "print":
        order.goods_status = "preparing"

async def repair_unhandled_orders(db: Optional[Session] = None):
    """Automatically check for unhandled PayPal orders from the last 3 days on startup"""
    from database import SessionLocal
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
        
    try:
        # Expand time range to 3 days to avoid missing orders after long downtime
        three_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
        stuck_orders = db.query(models.Order).filter(
            models.Order.status == "pending_payment",
            models.Order.paypal_order_id.isnot(None),
            models.Order.created_at >= three_days_ago
        ).all()

        for order in stuck_orders:
            try:
                confirm_paypal_payment(order, order.paypal_order_id)
                order.status = "paid"
                order.paid_at = datetime.datetime.now(datetime.timezone.utc)

                user = db.query(models.User).filter(models.User.id == order.user_id).first()
                if not user:
                    raise ValueError(f"User {order.user_id} not found")
                _activate_order_benefits(order, db, user)
                db.commit()
                print(f"Auto-repaired order {order.id} to paid status.")
            except Exception as e:
                db.rollback()
                print(f"Failed to auto-repair order {order.id}: {e}")
    finally:
        if should_close:
            db.close()

router = APIRouter(prefix="/api/orders", tags=["order"])

@router.get("", response_model=schemas.PaginatedOrders)
async def get_orders(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Get all orders for current user"""
    query = db.query(models.Order).filter(
        models.Order.user_id == current_user.id
    ).order_by(models.Order.created_at.desc())
    
    total = query.count()
    orders = query.offset((page - 1) * page_size).limit(page_size).all()
    
    for o in orders:
        o.address = db.query(models.ShippingAddress).filter(models.ShippingAddress.id == o.address_id).first()
        o.items = presign_order_items(db.query(models.OrderItem).filter(models.OrderItem.order_id == o.id).all())
    
    return backend_utils.paginate_response(orders, total, page, page_size)

@router.get("/model-stock")
async def get_model_stock(order_type: Optional[str] = None, db: Session = Depends(get_db)):
    """Get model stock status"""
    query = db.query(models.ModelSalesLimit)
    if order_type:
        query = query.filter(models.ModelSalesLimit.order_type == order_type)
    limits = query.all()
    result = []

    for limit in limits:
        result.append({
            "model_type": limit.model_type,
            "available": limit.stock > 0,
            "price": limit.price
        })
    return result

@router.post("", response_model=schemas.OrderResponse)

async def create_order(
    req: schemas.OrderCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Create new order (for direct links/Pro subscriptions)"""
    now = datetime.datetime.now(datetime.timezone.utc)
    today_start = datetime.datetime(now.year, now.month, now.day, tzinfo=datetime.timezone.utc)
    
    order_count = db.query(models.Order).filter(
        models.Order.user_id == current_user.id,
        models.Order.created_at >= today_start
    ).count()
    
    if order_count >= 100:
        raise HTTPException(status_code=429, detail="Daily order limit reached (100 times)")

    if req.order_type == "subscription":
        limit_cfg = db.query(models.ModelSalesLimit).filter(
            models.ModelSalesLimit.model_type == req.model_type,
            models.ModelSalesLimit.order_type == req.order_type
        ).first()
        if not limit_cfg:
             raise HTTPException(status_code=400, detail="Invalid subscription type")
        price = limit_cfg.price

        
        order = models.Order(
            user_id=current_user.id,
            address_id=None,
            order_type="subscription",
            status="pending_payment",
            price=price,
            shipping_fee=0.0,
            total_price=price
        )
        db.add(order)
        db.flush() # Generate order.id

        item = models.OrderItem(
            order_id=order.id,
            skin_url=None,
            model_type=req.model_type,
            price=price
        )
        db.add(item)
    else:
        # Direct link print order creation
        address = db.query(models.ShippingAddress).filter(
            models.ShippingAddress.id == req.address_id,
            models.ShippingAddress.user_id == current_user.id
        ).first()
        if not address:
            raise HTTPException(status_code=400, detail="Invalid shipping address or address does not belong to you")

        items_to_create = []

        # Check stock from DB
        limit_cfg = db.query(models.ModelSalesLimit).filter(
            models.ModelSalesLimit.model_type == req.model_type,
            models.ModelSalesLimit.order_type == req.order_type
        ).first()
        if not limit_cfg:
            raise HTTPException(status_code=400, detail="Invalid model type")
        stock = limit_cfg.stock

        if stock <= 0:
            raise HTTPException(status_code=400, detail="This model is sold out")

        if req.log_id:
            log_entry = db.query(models.GenerationLog).filter(
                models.GenerationLog.id == req.log_id,
                models.GenerationLog.is_deleted == False,
                models.GenerationLog.status == "success"
            ).first()
            if not log_entry:
                raise HTTPException(status_code=400, detail="Invalid model reference log")
            if not log_entry.is_public and log_entry.user_id != current_user.id:
                raise HTTPException(status_code=403, detail="Unauthorized to use this private model for ordering")
            items_to_create.append({
                "log_entry": log_entry,
                "model_type": req.model_type,
                "price": limit_cfg.price
            })




        if not items_to_create:
            raise HTTPException(status_code=400, detail="Checkout queue is empty")

        # Merge with unpaid orders having the same shipping address
        existing_order = db.query(models.Order).filter(
            models.Order.user_id == current_user.id,
            models.Order.address_id == req.address_id,
            models.Order.status == "pending_payment",
            models.Order.order_type == "print"
        ).first()

        if existing_order:
            current_count = db.query(models.OrderItem).filter(models.OrderItem.order_id == existing_order.id).count()
            if current_count + len(items_to_create) > 10:
                raise HTTPException(status_code=400, detail="Item limit for unpaid orders reached")


        added_price = sum(item["price"] for item in items_to_create)


        if existing_order:
            order = existing_order
            order.price += added_price
            order.total_price += added_price
            order.paypal_order_id = None
        else:
            order = models.Order(
                user_id=current_user.id,
                address_id=req.address_id,
                order_type="print",
                status="pending_payment", 
                price=added_price,
                shipping_fee=0.0,
                total_price=added_price
            )
            db.add(order)
            db.flush()


        created_items = []
        for it in items_to_create:
            item_id = models.generate_base58_id()
            skin_url = None
            if it["log_entry"]:
                skin_url = clone_skin_for_order(it["log_entry"], order.id, item_id)
            
            order_item = models.OrderItem(
                id=item_id,
                order_id=order.id,
                skin_url=skin_url,
                model_type=it["model_type"],
                price=it["price"],
                refer_log_id=it.get("log_entry").id if it.get("log_entry") else None
            )
            db.add(order_item)
            created_items.append(order_item)



        item = created_items[0] if created_items else None # Keep item reference for compatibility

    
    db.commit()
    db.refresh(order)
    order.items = presign_order_items([item]) 
    return order

@router.get("/{id}", response_model=schemas.OrderResponse)
async def get_order(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Get order details"""
    order = db.query(models.Order).filter(
        models.Order.id == id,
        models.Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order.address = db.query(models.ShippingAddress).filter(models.ShippingAddress.id == order.address_id).first()
    order.items = presign_order_items(db.query(models.OrderItem).filter(models.OrderItem.order_id == id).all())
    return order

@router.put("/{id}/cancel", response_model=schemas.OrderResponse)
async def cancel_order(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Cancel order"""
    order = db.query(models.Order).filter(
        models.Order.id == id,
        models.Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status == "cancelled":
        return order

    if order.status not in ["pending_payment"]:
         raise HTTPException(status_code=400, detail="Current order status cannot be cancelled")

    order.status = "cancelled"
    db.commit()
    db.refresh(order)
    return order

@router.delete("/{id}")
async def delete_order(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Delete cancelled order and S3 data"""
    order = db.query(models.Order).filter(
        models.Order.id == id,
        models.Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != "cancelled":
         raise HTTPException(status_code=400, detail="Only cancelled orders can be deleted")

    items = db.query(models.OrderItem).filter(models.OrderItem.order_id == id).all()
    
    for item in items:
        if item.skin_url:
            try:
                # skin_url typically is orders/xxx/yyy.png
                s3_client.delete_object(
                    Bucket=settings.AWS_PRIVATE_BUCKET_NAME,
                    Key=item.skin_url
                )
            except Exception as e:
                print(f"Failed to delete S3 file {item.skin_url}: {e}")
                # We can choose to continue or abort. Let's continue to delete as much as possible,
                # but maybe we should let database delete happen.
                
        db.delete(item)

    db.delete(order)
    db.commit()
    return {"status": "success", "message": f"Order deleted"}

@router.post("/{id}/pay", response_model=schemas.OrderResponse)
async def pay_order(
    id: str,
    req: schemas.PayRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Confirm PayPal payment for an order."""
    order = db.query(models.Order).filter(
        models.Order.id == id,
        models.Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if order.status != "pending_payment":
         raise HTTPException(status_code=400, detail="Order is not in pending payment status")

    try:
        confirm_paypal_payment(order, req.paypal_order_id)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error processing PayPal order: {e}")
        raise HTTPException(status_code=500, detail="Payment confirmation failed")

    order.status = "paid"
    order.paid_at = datetime.datetime.now(datetime.timezone.utc)
    _activate_order_benefits(order, db, current_user)
    
    # Deduct model static stock
    if order.order_type == "print":
        items = db.query(models.OrderItem).filter(models.OrderItem.order_id == id).all()
        for it in items:
            limit_cfg = db.query(models.ModelSalesLimit).filter(
                models.ModelSalesLimit.model_type == it.model_type
            ).first()
            if limit_cfg:
                limit_cfg.stock -= 1

    db.commit()

    db.refresh(order)
    order.items = presign_order_items(db.query(models.OrderItem).filter(models.OrderItem.order_id == id).all())
    return order

@router.delete("/items/{item_id}")
async def delete_order_item(
    item_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Remove an item from an unpaid order"""
    item = db.query(models.OrderItem).filter(models.OrderItem.id == item_id).first()
    if not item:
         raise HTTPException(status_code=404, detail="Order item not found")
         
    order = db.query(models.Order).filter(models.Order.id == item.order_id, models.Order.user_id == current_user.id).first()
    if not order:
         raise HTTPException(status_code=404, detail="Order not found or unauthorized access")
         
    if order.status != "pending_payment":
         raise HTTPException(status_code=400, detail="Items can only be deleted from pending payment orders")
         
    # Deduct amount, clamped to zero
    order.price = max(0.0, order.price - item.price)
    order.total_price = max(0.0, order.total_price - item.price)
    order.paypal_order_id = None
    
    if item.skin_url:
        try:
            s3_client.delete_object(
                Bucket=settings.AWS_PRIVATE_BUCKET_NAME,
                Key=item.skin_url
            )
        except Exception as e:
            print(f"Failed to delete S3 file {item.skin_url}: {e}")
            
    db.delete(item)
    db.flush() # Flush session state to get correct remaining items count
    
    remaining_count = db.query(models.OrderItem).filter(models.OrderItem.order_id == order.id).count()
    if remaining_count == 0:
        # If no items left, cancel the order
        order.status = "cancelled"
        
    db.commit()
    return {"status": "success", "message": "Item deleted"}

@router.post("/{id}/create-paypal-order")
async def create_paypal_order(
    id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Create PayPal order"""
    order = db.query(models.Order).filter(
        models.Order.id == id,
        models.Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.status != "pending_payment":
        raise HTTPException(status_code=400, detail="Order is not in pending payment status")
        
    try:
        # Pass order.id for bidirectional binding
        paypal_order = create_paypal_order_api(order.total_price, order.id)
        # Save to database
        order.paypal_order_id = paypal_order["id"]
        db.commit()
        return {"id": paypal_order["id"]}
    except Exception as e:
        print(f"Error creating PayPal order: {e}")
        raise HTTPException(status_code=500, detail="Failed to create payment order")


@router.get("/paypal/config")
async def get_paypal_config():
    """Get PayPal Client ID and Plan IDs"""
    return {
        "client_id": settings.PAYPAL_CLIENT_ID,
        "pro_plus_plan_id": settings.PAYPAL_PRO_PLUS_PLAN_ID,
        "pro_max_plan_id": settings.PAYPAL_PRO_MAX_PLAN_ID
    }

@router.post("/subscription/activate")
async def activate_subscription(
    req: schemas.PayRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user)
):
    """Activate subscription after PayPal approval"""
    if not req.paypal_order_id:
        raise HTTPException(status_code=400, detail="Subscription ID required")

    sub_id = req.paypal_order_id # We reuse paypal_order_id field for subscription ID in PayRequest
    _validate_paypal_id(sub_id, "Subscription ID")
    
    try:
        existing_owner = db.query(models.User).filter(
            models.User.paypal_subscription_id == sub_id,
            models.User.id != current_user.id,
        ).first()
        if existing_owner:
            raise HTTPException(status_code=403, detail="Subscription is already linked to another user")

        sub_data = get_paypal_subscription_api(sub_id)
        status = sub_data.get("status")
        
        if status != "ACTIVE":
            raise HTTPException(status_code=400, detail=f"Subscription status is {status}")

        actual_plan_id = sub_data.get("plan_id")
        pro_level = _paypal_plan_to_pro_level(actual_plan_id)
        if not pro_level:
            raise HTTPException(status_code=400, detail="Subscription plan is not supported")

        subscription_custom_id = _subscription_custom_id(sub_data)
        if subscription_custom_id:
            if subscription_custom_id != current_user.id:
                raise HTTPException(status_code=403, detail="Subscription does not belong to current user")
        elif current_user.paypal_subscription_id != sub_id:
            subscriber_email = _subscription_subscriber_email(sub_data)
            if not subscriber_email:
                raise HTTPException(status_code=400, detail="Subscription subscriber email is missing")
            if subscriber_email != current_user.email.lower():
                raise HTTPException(status_code=403, detail="Subscription does not belong to current user")

        current_user.paypal_subscription_id = sub_id
        current_user.paypal_subscription_status = "ACTIVE"

        billing_info = sub_data.get("billing_info", {})
        next_billing_time_str = billing_info.get("next_billing_time")
        if next_billing_time_str:
            # Add a 3 day grace period to the exact next billing cycle.
            dt = datetime.datetime.fromisoformat(next_billing_time_str.replace("Z", "+00:00"))
            current_user.pro_expires_at = dt + datetime.timedelta(days=3)
        else:
            now = datetime.datetime.now(datetime.timezone.utc)
            if not current_user.pro_expires_at or current_user.pro_expires_at < now:
                current_user.pro_expires_at = now + datetime.timedelta(days=31)
            else:
                current_user.pro_expires_at = current_user.pro_expires_at + datetime.timedelta(days=31)

        current_user.pro_level = pro_level
        
        # Award monthly credits immediately
        backend_utils.award_subscription_credits(db, current_user, pro_level, sub_id, is_webhook=False)
                 
        db.commit()
        db.refresh(current_user)
        return {"status": "success", "subscription_id": sub_id}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Failed to activate subscription: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify subscription")
