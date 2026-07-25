"""Credit purchase / top-up endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional
import uuid

from database import get_db
import models
import auth
from payment_utils import create_paypal_order_api, capture_paypal_order_api, get_paypal_order_api

router = APIRouter(prefix="/api/credits", tags=["credits"])

CREDIT_RATE = 10  # 1 USD = 10 credits
MIN_AMOUNT = 1    # minimum $1


class CreditPurchaseRequest(BaseModel):
    amount: float = Field(..., description="Dollar amount to purchase")
    return_url: Optional[str] = Field(None, description="URL to redirect after PayPal approval")


class CreditCaptureRequest(BaseModel):
    paypal_order_id: str = Field(..., description="PayPal order ID to capture")


@router.get("/packages")
async def get_credit_packages():
    """Return reference credit packages for the frontend."""
    return [
        {"dollars": 1, "credits": 10},
        {"dollars": 5, "credits": 50},
        {"dollars": 10, "credits": 100},
        {"dollars": 20, "credits": 200},
    ]


@router.post("/purchase")
async def purchase_credits(
    req: CreditPurchaseRequest,
    current_user: models.User = Depends(auth.get_current_user),
):
    """Create a PayPal order for a credit purchase."""
    if req.amount < MIN_AMOUNT:
        raise HTTPException(status_code=400, detail=f"Minimum purchase amount is ${MIN_AMOUNT}")

    credits = int(req.amount * CREDIT_RATE)

    # Bind the user and credit amount into the PayPal order custom_id
    purchase_id = f"credit:{current_user.id}:{credits}:{uuid.uuid4().hex[:8]}"

    try:
        paypal_order = create_paypal_order_api(
            req.amount,
            purchase_id,
            return_url=req.return_url,
            cancel_url=req.return_url,  # same URL, page will check params
        )

        # Extract approval URL from PayPal response
        approval_url = None
        for link in paypal_order.get("links", []):
            if link.get("rel") == "approve":
                approval_url = link.get("href")
                break

        return {
            "paypal_order_id": paypal_order["id"],
            "approval_url": approval_url or f"https://www.paypal.com/checkoutnow?token={paypal_order['id']}",
            "amount": req.amount,
            "credits": credits,
        }
    except Exception as e:
        print(f"Error creating PayPal order for credits: {e}")
        raise HTTPException(status_code=500, detail="Failed to create payment order")


@router.post("/capture")
async def capture_credits(
    req: CreditCaptureRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_user),
):
    """Capture a PayPal order and award credits."""
    paypal_order_id = req.paypal_order_id.strip()
    if not paypal_order_id or len(paypal_order_id) > 100 or not paypal_order_id.isascii():
        raise HTTPException(status_code=400, detail="Invalid PayPal order ID")

    # Fetch the PayPal order to verify it and extract metadata
    try:
        paypal_order = get_paypal_order_api(paypal_order_id)
    except Exception as e:
        print(f"Error fetching PayPal order for credits: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch payment order")

    # Extract custom_id from purchase unit
    purchase_units = paypal_order.get("purchase_units") or []
    if not purchase_units:
        raise HTTPException(status_code=400, detail="PayPal order is missing purchase units")
    unit = purchase_units[0]
    custom_id = unit.get("custom_id", "")

    # Parse and validate credit amount from custom_id: credit:{user_id}:{credits}:{random}
    if not custom_id or not custom_id.startswith("credit:"):
        raise HTTPException(status_code=400, detail="Invalid payment details for credit purchase")

    parts = custom_id.split(":")
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="Invalid payment custom ID format")

    try:
        credits_to_award = int(parts[2])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid credit amount in payment custom ID")

    # Verify ownership
    if parts[1] != current_user.id:
        raise HTTPException(status_code=403, detail="Payment does not belong to this user")

    # Check payment status and capture if needed
    status = paypal_order.get("status")
    if status == "APPROVED":
        try:
            paypal_order = capture_paypal_order_api(paypal_order_id)
        except Exception as e:
            print(f"Error capturing PayPal order for credits: {e}")
            raise HTTPException(status_code=500, detail="Failed to capture payment")

    # Verify capture is completed
    capture_completed = False
    if paypal_order.get("status") == "COMPLETED":
        capture_completed = True
    else:
        captures = unit.get("payments", {}).get("captures", [])
        if captures and captures[0].get("status") == "COMPLETED":
            capture_completed = True

    if not capture_completed:
        raise HTTPException(status_code=400, detail="Payment not completed")

    # Check for duplicate capture (idempotency)
    existing = (
        db.query(models.CreditLog)
        .filter(
            models.CreditLog.user_id == current_user.id,
            models.CreditLog.action == "purchase",
            models.CreditLog.source == f"PayPal: {paypal_order_id}",
        )
        .first()
    )
    if existing:
        return {
            "credits_awarded": existing.amount,
            "new_balance": current_user.credits,
            "duplicate": True,
        }

    # Award credits
    current_user.credits = (current_user.credits or 0) + credits_to_award

    # Create credit log
    log = models.CreditLog(
        user_id=current_user.id,
        amount=credits_to_award,
        action="purchase",
        source=f"PayPal: {paypal_order_id}",
    )
    db.add(log)
    db.commit()
    db.refresh(current_user)

    return {
        "credits_awarded": credits_to_award,
        "new_balance": current_user.credits,
    }
