import requests
import base64
from config import settings

PAYPAL_TIMEOUT_SECONDS = 15

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

def create_paypal_order_api(amount: float, order_id: str, currency_code: str = "USD"):
    """Create order using PayPal API."""
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
    
    response = requests.post(
        f"{settings.PAYPAL_API_BASE}/v2/checkout/orders",
        headers=headers,
        json=order_payload,
        timeout=PAYPAL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

def capture_paypal_order_api(paypal_order_id: str):
    """Capture payment after user approves."""
    access_token = get_paypal_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
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
