"""PayPal Payouts transport. Never log credentials, recipients, or response bodies."""
import re
from urllib.parse import urlparse
import requests
from config import settings
from payment_utils import get_paypal_access_token, PAYPAL_TIMEOUT_SECONDS


class PayoutTransportError(Exception):
    def __init__(self, code, definitive=False):
        self.code, self.definitive = code, definitive
        super().__init__(code)


def headers(request_id=None):
    result = {"Authorization": f"Bearer {get_paypal_access_token()}", "Content-Type": "application/json"}
    if request_id: result["PayPal-Request-Id"] = request_id
    return result


def get_batch(batch_id):
    if not re.fullmatch(r"[A-Za-z0-9]+", batch_id):
        raise PayoutTransportError("paypal_invalid_batch")
    response = requests.get(f"{settings.PAYPAL_API_BASE}/v1/payments/payouts/{batch_id}",
                            headers=headers(), params={"page_size": 1000}, timeout=PAYPAL_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def create_batch(payout_id, amount_cents, email):
    sender_id = f"creator-{payout_id}"
    response = requests.post(f"{settings.PAYPAL_API_BASE}/v1/payments/payouts", headers=headers(sender_id),
        json={"sender_batch_header": {"sender_batch_id": sender_id, "email_subject": "Your EntropyDrop creator commission"},
              "items": [{"recipient_type": "EMAIL", "recipient_wallet": "PAYPAL", "receiver": email,
                         "sender_item_id": payout_id, "amount": {"value": f"{amount_cents // 100}.{amount_cents % 100:02d}", "currency": "USD"}}]},
        timeout=PAYPAL_TIMEOUT_SECONDS)
    if response.ok:
        return response.json()
    try: data = response.json()
    except ValueError: raise PayoutTransportError("paypal_unconfirmed")
    duplicate = data.get("name") == "DUPLICATE_BATCH_ID" or any(
        item.get("issue") == "DUPLICATE_BATCH_ID" for item in data.get("details", []) if isinstance(item, dict))
    if duplicate:
        # Only accept PayPal's own batch path; never fetch arbitrary returned URLs.
        for link in data.get("links", []):
            parsed = urlparse(link.get("href", ""))
            match = re.fullmatch(r"/v1/payments/payouts/([A-Za-z0-9]+)", parsed.path)
            if match and parsed.scheme == "https" and parsed.netloc == urlparse(settings.PAYPAL_API_BASE).netloc:
                return get_batch(match[1])
        raise PayoutTransportError("paypal_duplicate_unresolved")
    # Only documented request rejection errors release the reservation. All
    # ambiguous network/server responses stay reserved and use the same batch ID.
    definitive = response.status_code in (400, 401, 403, 422) and data.get("name") in {
        "VALIDATION_ERROR", "INVALID_REQUEST", "INSUFFICIENT_FUNDS", "AUTHORIZATION_ERROR", "NOT_AUTHORIZED", "PERMISSION_DENIED"}
    raise PayoutTransportError("paypal_rejected" if definitive else "paypal_unconfirmed", definitive)
