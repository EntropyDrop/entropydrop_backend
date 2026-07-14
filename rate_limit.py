from fastapi import Request
from slowapi import Limiter
import ipaddress
from config import settings


def _trusted_proxy_networks():
    networks = []
    for raw_cidr in settings.TRUSTED_PROXY_CIDRS.split(","):
        cidr = raw_cidr.strip()
        if not cidr:
            continue
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return networks


def _client_host(request: Request) -> str:
    if request.client:
        return request.client.host
    return "127.0.0.1"


def _is_trusted_proxy(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in network for network in _trusted_proxy_networks())


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate

def get_real_remote_address(request: Request) -> str:
    """
    Get the client IP address, only trusting proxy headers from configured proxies.
    """
    client_host = _client_host(request)
    if not _is_trusted_proxy(client_host):
        return client_host

    real_ip = _valid_ip(request.headers.get("X-Real-IP"))
    if real_ip:
        return real_ip

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        forwarded_ip = _valid_ip(forwarded_for.split(",")[0])
        if forwarded_ip:
            return forwarded_ip

    return client_host

limiter = Limiter(
    key_func=get_real_remote_address,
    default_limits=["60/minute", "1000/hour", "4000/day"]
)
