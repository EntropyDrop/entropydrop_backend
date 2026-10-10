"""PayPal shipping rules, supplemented by its referenced postal metadata.

No customer address is sent to an external validation service. The frontend
bundles the same version of data/shipping-address-rules.json.
"""
import json
import re
import unicodedata
from pathlib import Path

RULES = json.loads((Path(__file__).parent / 'data/shipping-address-rules.json').read_text())
ADDRESS_FIELDS = ('recipient_name', 'country', 'state', 'city', 'zip_code', 'detail_address', 'phone')


class ShippingAddressError(ValueError):
    def __init__(self, fields):
        self.fields = fields
        super().__init__('Please correct the shipping address: ' + ', '.join(fields))

    @property
    def detail(self):
        return {'code': 'invalid_shipping_address', 'fields': self.fields}


def normalize_address(data):
    result = {key: str(data.get(key) or '').strip() for key in ADDRESS_FIELDS}
    country = result['country'].upper()
    result['country'] = {'C2': 'CN', 'UK': 'GB'}.get(country, country)
    result['zip_code'] = unicodedata.normalize('NFKC', result['zip_code']).upper()
    rule = RULES['countries'].get(result['country'], {})
    for code, name in rule.get('states', {}).items():
        if result['state'].casefold() in (code.casefold(), name.casefold()):
            result['state'] = code
            break
    return result


def street_lines(street):
    lines = [line.strip() for line in street.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
    if not lines:
        return '', ''
    first, rest = lines[0], ' '.join(lines[1:])
    if len(first) > 300:
        split = first.rfind(' ', 0, 301)
        if split <= 0 or len(' '.join(filter(None, [first[split:].strip(), rest]))) > 300:
            split = 300
        first, rest = first[:split], ' '.join(filter(None, [first[split:].strip(), rest]))
    return first, rest


def validate_address(data):
    result = normalize_address(data)
    errors = {}
    rule = RULES['countries'].get(result['country'])
    for field in ('recipient_name', 'country', 'detail_address', 'phone'):
        if not result[field]:
            errors[field] = 'required'
    if result['country'] and not rule:
        errors['country'] = 'country_invalid'
    for field, limit in RULES['limits'].items():
        if len(result[field]) > limit:
            errors[field] = 'too_long'
    if rule:
        for field, required in (('state', rule['state_required']), ('city', rule['city_required']), ('zip_code', rule['postal_required'])):
            if required and not result[field]:
                errors[field] = 'required'
        if result['state'] and rule.get('states') and result['state'] not in rule['states']:
            errors['state'] = 'state_invalid'
        postal = result['zip_code']
        if postal and not rule['postal_visible']:
            errors['zip_code'] = 'postal_unused'
        elif postal and rule['postal_pattern'] and not re.fullmatch(rule['postal_pattern'], postal, re.ASCII | re.IGNORECASE):
            errors['zip_code'] = 'postal_format'
    first, second = street_lines(result['detail_address'])
    if len(first) > 300 or len(second) > 300 or len(result['detail_address']) > 600:
        errors['detail_address'] = 'street_length'
    if errors:
        raise ShippingAddressError(errors)
    return result


def paypal_shipping_address(snapshot):
    data = validate_address(snapshot or {})
    first, second = street_lines(data['detail_address'])
    address = {'country_code': RULES['countries'][data['country']]['paypal_country_code'], 'address_line_1': first}
    if second:
        address['address_line_2'] = second
    for source, target in (('state', 'admin_area_1'), ('city', 'admin_area_2'), ('zip_code', 'postal_code')):
        if data[source]:
            address[target] = data[source]
    return address
