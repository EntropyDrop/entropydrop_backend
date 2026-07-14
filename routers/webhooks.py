from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from database import get_db
import models
import datetime
from config import settings
from rate_limit import limiter

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _paypal_plan_to_pro_level(plan_id: str):
    if plan_id and plan_id == settings.PAYPAL_PRO_MAX_PLAN_ID:
        return "pro-max"
    if plan_id and plan_id == settings.PAYPAL_PRO_PLUS_PLAN_ID:
        return "pro-plus"
    return None

@router.post("/paypal")
@limiter.exempt
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    from payment_utils import verify_paypal_webhook_signature
    
    # 1. Get raw body and headers
    body = await request.body()
    headers = dict(request.headers)
    
    # 2. Verify Webhook Signature
    if not settings.PAYPAL_WEBHOOK_ID:
        raise HTTPException(status_code=400, detail="PayPal webhook ID is not configured")

    is_valid = verify_paypal_webhook_signature(headers, body, settings.PAYPAL_WEBHOOK_ID)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
        
    payload = await request.json()
    event_type = payload.get("event_type")
    resource = payload.get("resource", {})
    
    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        sub_id = resource.get("id")
        user = db.query(models.User).filter(models.User.paypal_subscription_id == sub_id).first()
        if user:
            user.paypal_subscription_status = "ACTIVE"
            db.commit()
            
    elif event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        sub_id = resource.get("id")
        user = db.query(models.User).filter(models.User.paypal_subscription_id == sub_id).first()
        if user:
            user.paypal_subscription_status = "CANCELLED"
            db.commit()
            
    elif event_type == "PAYMENT.SALE.COMPLETED":
        # Payment succeeded! Add 31 days to Pro status and record an Order
        sub_id = resource.get("billing_agreement_id")
        sale_id = resource.get("id")
        
        # PayPal amount object looks like {"total": "20.00", "currency": "USD"}
        amount_total_str = resource.get("amount", {}).get("total", "20.0")
        try:
            amount_total = float(amount_total_str)
        except ValueError:
            return {"status": "failed"}

        user = db.query(models.User).filter(models.User.paypal_subscription_id == sub_id).first()
        if not user:
            return {"status": "failed"}

        now = datetime.datetime.now(datetime.timezone.utc)
        # 1. Fetch exact next billing time and plan ID from PayPal
        from payment_utils import get_paypal_subscription_api
        try:
            sub_data = get_paypal_subscription_api(sub_id)
            actual_plan_id = sub_data.get("plan_id")
            
            pro_level = _paypal_plan_to_pro_level(actual_plan_id)
            if not pro_level:
                print(f"Unknown plan ID: {actual_plan_id}")
                return {"status": "failed"}
                
            next_billing_str = sub_data.get("billing_info", {}).get("next_billing_time")
            if next_billing_str:
                dt = datetime.datetime.fromisoformat(next_billing_str.replace("Z", "+00:00"))
                user.pro_expires_at = dt + datetime.timedelta(days=3)
            else:
                raise ValueError("No next_billing_time found")
        except Exception as e:
            raise HTTPException(status_code=400, detail="Failed to validate subscription payment")
            
        user.paypal_subscription_status = "ACTIVE"
        
        # 2. Check if this sale_id already exists to prevent duplicate webhooks
        existing_order = db.query(models.Order).filter(models.Order.paypal_order_id == sale_id).first()
        if not existing_order:
            # 3. Create a record in Order and OrderItem
            new_order = models.Order(
                user_id=user.id,
                order_type="subscription",
                status="paid",
                price=amount_total,
                shipping_fee=0.0,
                total_price=amount_total,
                paid_at=now,
                paypal_order_id=sale_id,
                goods_status=None,
                address_id=None
            )
            db.add(new_order)
            db.flush() # flush to get new_order.id
            
            new_order_item = models.OrderItem(
                order_id=new_order.id,
                model_type="pro-plus" if actual_plan_id == settings.PAYPAL_PRO_PLUS_PLAN_ID else ("pro-max" if actual_plan_id == settings.PAYPAL_PRO_MAX_PLAN_ID else "pro-plus"),
                price=amount_total,
                skin_url=None,
                refer_log_id=None
            )
            db.add(new_order_item)
            
            # Update user pro_level
            user.pro_level = pro_level
            
            # Award monthly credits immediately
            import backend_utils
            backend_utils.award_subscription_credits(db, user, pro_level, sub_id, is_webhook=True)
        
        db.commit()
        print(f"Granted 31 days to user {user.id} and recorded order for sale {sale_id}")
                
    return {"status": "success"}
