import requests
import base64
from config import settings

PAYPAL_TIMEOUT_SECONDS = 15
# Marks purchase units created with the website address locked in PayPal.
PAYPAL_WEBSITE_SHIPPING_REFERENCE = "website-shipping-v2"

def get_paypal_access_token():
    """Get OAuth2 token from PayPal."""
    auth = f"{settings.PAYPAL_CLIENT_ID}:{settings.PAYPAL_SECRET}"
    auth_b64 = base64.b64encode(auth.encode("utf-8")).decode("utf-8")
    
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v1/oauth2/token",
        headers=headers,
        data=data,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()["access_token"]

def create_paypal_order_api(amount: float, order_id: str, currency_code: str = "USD", return_url: str | None = None, cancel_url: str | None = None, shipping_address: dict | None = None, shipping_name: str | None = None):
    """Create an order, optionally locking shipping to the merchant address."""
    if shipping_address is not None:
        shipping_name = (shipping_name or "").strip()
        if not shipping_name or len(shipping_name) > 300:
            raise ValueError("Shipping recipient name must contain 1 to 300 characters")
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    order_payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": order_id, # Bidirectional binding: associated local order ID
                "amount": {
                    "currency_code": currency_code,
                    "value": f"{amount:.2f}"
                }
            }
        ]
    }
    experience_context = {}
    if return_url:
        experience_context["return_url"] = return_url
    if cancel_url:
        experience_context["cancel_url"] = cancel_url
    if shipping_address is not None:
        unit = order_payload["purchase_units"][0]
        unit["reference_id"] = PAYPAL_WEBSITE_SHIPPING_REFERENCE
        unit["shipping"] = {"name": {"full_name": shipping_name}, "address": shipping_address}
        experience_context["shipping_preference"] = "SET_PROVIDED_ADDRESS"
        order_payload["payment_source"] = {"paypal": {"experience_context": experience_context}}
    elif experience_context:
        # Preserve existing Credits / non-shipping checkout behavior.
        order_payload["application_context"] = experience_context
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v2/checkout/orders",
        headers=headers,
        json=order_payload,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def capture_paypal_order_api(paypal_order_id: str):
    """Capture payment after user approves, requesting the full order details."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v2/checkout/orders/{paypal_order_id}/capture",
        headers=headers,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def get_paypal_order_api(paypal_order_id: str):
    """Get order details from PayPal."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    response = requests.get(
        f"{settings.PAYPAL_API_BASE}/v2/checkout/orders/{paypal_order_id}",
        headers=headers,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def get_paypal_subscription_api(subscription_id: str):
    """Get subscription details from PayPal."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    response = requests.get(
        f"{settings.PAYPAL_API_BASE}/v1/billing/subscriptions/{subscription_id}",
        headers=headers,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def create_paypal_subscription_api(plan_id: str, custom_id: str, return_url: str, cancel_url: str):
    """Create subscription using PayPal API."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "plan_id": plan_id,
        "custom_id": custom_id,
        "application_context": {
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url
        }
    }
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v1/billing/subscriptions",
        headers=headers,
        json=payload,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def revise_paypal_subscription_api(subscription_id: str, plan_id: str):
    """Revise subscription using PayPal API."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "plan_id": plan_id
    }
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v1/billing/subscriptions/{subscription_id}/revise",
        headers=headers,
        json=payload,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def cancel_paypal_subscription_api(subscription_id: str, reason: str = "User requested cancellation"):
    """Cancel subscription via PayPal."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "reason": reason
    }

    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v1/billing/subscriptions/{subscription_id}/cancel",
        headers=headers,
        json=payload,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    # 204 No Content expected on success
    return True

def verify_paypal_webhook_signature(headers: dict, body: bytes, webhook_id: str):
    """Verify PayPal Webhook Signature"""
    if not webhook_id:
        return False

    access_token = get_paypal_access_token()

    normalized_headers = {str(key).lower(): value for key, value in headers.items()}
    
    auth_algorithm = normalized_headers.get("paypal-auth-algo")
    cert_url = normalized_headers.get("paypal-cert-url")
    transmission_id = normalized_headers.get("paypal-transmission-id")
    transmission_sig = normalized_headers.get("paypal-transmission-sig")
    transmission_time = normalized_headers.get("paypal-transmission-time")
    
    if not all([auth_algorithm, cert_url, transmission_id, transmission_sig, transmission_time]):
        return False
        
    import json
    
    verify_payload = {
        "auth_algo": auth_algorithm,
        "cert_url": cert_url,
        "transmission_id": transmission_id,
        "transmission_sig": transmission_sig,
        "transmission_time": transmission_time,
        "webhook_id": webhook_id,
        "webhook_event": json.loads(body)
    }
    
    resp_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    verify_resp = requests.post(
        f"{settings.PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
        json=verify_payload,
        headers=resp_headers,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    
    if verify_resp.status_code == 200:
        return verify_resp.json().get("verification_status") == "SUCCESS"
    return False

def get_paypal_transactions_api(start_date: str, end_date: str):
    """
    Get transactions from PayPal Reporting API.
    start_date: ISO 8601 string (e.g. 2024-05-01T00:00:00Z)
    end_date: ISO 8601 string (e.g. 2024-05-03T23:59:59Z)
    """
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    endpoint = f"{settings.PAYPAL_API_BASE}/v1/reporting/transactions"
    page = 1
    total_pages = 1
    merged_details = []
    last_payload = {}

    while page <= total_pages and page <= 20:
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "fields": "all",
            "page_size": 500,
            "page": page,
        }

        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=PAYPAL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        last_payload = payload
        merged_details.extend(payload.get("transaction_details", []))

        try:
            total_pages = int(payload.get("total_pages") or page)
        except (TypeError, ValueError):
            total_pages = page
        page += 1

    last_payload["transaction_details"] = merged_details
    return last_payload

def anonymize_email(email: str) -> str:
    """Anonymize payer email for public ledger"""
    if not email or "@" not in email:
        return "***"
    try:
        parts = email.split("@")
        name = parts[0]
        domain = parts[1]
        if len(name) <= 2:
            masked_name = name[0] + "*" * (len(name) - 1)
        else:
            masked_name = name[:2] + "***"
        return f"{masked_name}@{domain}"
    except Exception:
        return "***"

def anonymize_name(given_name: str, surname: str) -> str:
    """Anonymize payer name for public ledger"""
    if not given_name and not surname:
        return "User"
    g = given_name or ""
    s = surname or ""
    try:
        if g:
            g = g[0] + "***" if len(g) > 1 else g
        if s:
            s = s[0] + "***" if len(s) > 1 else s
        return f"{g} {s}".strip()
    except Exception:
        return "User"


def refund_paypal_capture_api(capture_id: str, amount: str, request_id: str):
    """Refund the exact captured order amount with a stable idempotency key."""
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v2/payments/captures/{capture_id}/refund",
        headers={"Authorization": f"Bearer {get_paypal_access_token()}",
                 "Content-Type": "application/json", "PayPal-Request-Id": request_id,
                 "Prefer": "return=representation"},
        json={"amount": {"value": amount, "currency_code": "USD"}},
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def get_paypal_refund_api(refund_id: str):
    response = requests.get(
        f"{settings.PAYPAL_API_BASE}/v2/payments/refunds/{refund_id}",
        headers={"Authorization": f"Bearer {get_paypal_access_token()}"},
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()
