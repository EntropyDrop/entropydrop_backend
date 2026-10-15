"""Map only the verified PayPal order's shipping address into an order snapshot."""
from shipping_address_rules import ShippingAddressError, validate_address


def _object(value):
    return value if isinstance(value, dict) else {}


def paypal_address_snapshot(payload, order_id, user_id):
    units = payload.get("purchase_units")
    if not isinstance(units, list) or len(units) != 1:
        raise ShippingAddressError({"address": "invalid_purchase_units"})
    shipping = _object(_object(units[0]).get("shipping"))
    address = _object(shipping.get("address"))
    name = _object(shipping.get("name"))
    phone = _object(shipping.get("phone_number"))
    first_line = address.get("address_line_1")
    second_line = address.get("address_line_2", "")
    if not isinstance(first_line, str) or not first_line.strip() or not isinstance(second_line, str):
        raise ShippingAddressError({"detail_address": "required"})
    fields = {
        "recipient_name": name.get("full_name", ""),
        "country": address.get("country_code", ""),
        "state": address.get("admin_area_1", ""),
        "city": address.get("admin_area_2", ""),
        "zip_code": address.get("postal_code", ""),
        "detail_address": "\n".join(line for line in (first_line, second_line) if line.strip()),
        "phone": phone.get("national_number", ""),
    }
    if not all(isinstance(value, str) for value in fields.values()):
        raise ShippingAddressError({"address": "invalid_fields"})
    # PayPal does not guarantee a recipient phone. Preserve an omitted phone
    # as empty; never substitute billing details or make up contact data.
    if fields["phone"] and isinstance(phone.get("country_code"), str) and phone["country_code"]:
        fields["phone"] = f'+{phone["country_code"]} {fields["phone"]}'
    normalized = validate_address(fields, require_phone=False)
    return {**normalized, "id": f"paypal:{order_id}", "user_id": user_id, "is_default": False}
