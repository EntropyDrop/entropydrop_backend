from fastapi import Request
from slowapi import Limiter
import ipaddress
import itertools
import jwt
from config import settings


class HeaderSafeLimiter(Limiter):
    """Let middleware add headers when an endpoint returns a plain JSON value."""

    def _check_request_limit(self, request, endpoint_func, in_middleware=True):
        # SlowAPI's middleware skips routes that use @limit, and its decorator
        # normally checks route/default limits only. Explicitly evaluate the
        # application-wide IP budget as well so marked routes cannot bypass it,
        # but avoid re-checking default limits that the decorator intentionally overrides.
        if not in_middleware and self.enabled and self._application_limits:
            endpoint_func_name = (
                f"{endpoint_func.__module__}.{endpoint_func.__name__}"
                if endpoint_func
                else ""
            )
            if (
                endpoint_func_name
                and endpoint_func_name not in self._exempt_routes
                and not any(fn() for fn in self._request_filters)
            ):
                app_limits = list(itertools.chain(*self._application_limits))
                self._Limiter__evaluate_limits(request, "global", app_limits)
        return super()._check_request_limit(request, endpoint_func, in_middleware)

    def _inject_headers(self, response, current_limit):
        # SlowAPI's decorator tries to inject into an explicit ``response``
        # argument before FastAPI has converted a dict/model to a Response. The
        # outer middleware performs the same injection on the real Response.
        if response is None:
            return response
        try:
            return super()._inject_headers(response, current_limit)
        except Exception:
            # Header telemetry must never turn a successfully handled request
            # into a 500 when the rate-limit backend cannot report its window.
            self.logger.warning("Could not add rate-limit response headers", exc_info=True)
            return response


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
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return str(address)


def _forwarded_client_ip(value: str | None) -> str | None:
    """Return the first untrusted hop in a validated X-Forwarded-For chain."""
    if not value:
        return None

    addresses = []
    for raw_address in value.split(","):
        address = _valid_ip(raw_address)
        if address is None:
            # Do not partially trust a malformed chain. A trusted proxy may still
            # provide a separately validated X-Real-IP fallback.
            return None
        addresses.append(address)

    # Proxies append addresses on the right. Peeling trusted hops from that end
    # prevents a client-supplied prefix from becoming the rate-limit identity.
    for address in reversed(addresses):
        if not _is_trusted_proxy(address):
            return address
    return addresses[0] if addresses else None


def get_real_remote_address(request: Request) -> str:
    """
    Get the client IP address, only trusting proxy headers from configured proxies.
    """
    client_host = _client_host(request)
    if not _is_trusted_proxy(client_host):
        return client_host

    forwarded_ip = _forwarded_client_ip(request.headers.get("X-Forwarded-For"))
    if forwarded_ip:
        return forwarded_ip

    real_ip = _valid_ip(request.headers.get("X-Real-IP"))
    if real_ip:
        return real_ip

    return client_host


def get_authenticated_or_remote_address(request: Request) -> str:
    """Use the authenticated account/session when available, otherwise the IP."""
    principal = getattr(request.state, "rate_limit_principal", None)
    if principal:
        return str(principal)

    cookie_name = getattr(settings, "AUTH_SESSION_COOKIE_NAME", "")
    raw_session = request.cookies.get(cookie_name) if cookie_name else None
    if raw_session and raw_session.count(".") == 1:
        session_id = raw_session.split(".", 1)[0]
        if 16 <= len(session_id) <= 64:
            return f"session:{session_id}"

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            if payload.get("type") in (None, "access") and payload.get("sub"):
                return f"user:{payload['sub']}"
        except jwt.PyJWTError:
            pass

    return get_real_remote_address(request)


async def ensure_rate_limit_headers(request: Request, call_next):
    """Add headers for decorated routes that return dicts or Pydantic models."""
    response = await call_next(request)
    current_limit = getattr(request.state, "view_rate_limit", None)
    if current_limit is not None and "X-RateLimit-Limit" not in response.headers:
        return request.app.state.limiter._inject_headers(response, current_limit)
    return response


limiter = HeaderSafeLimiter(
    key_func=get_real_remote_address,
    default_limits=["60/minute", "1000/hour", "4000/day"],
    application_limits=["300/minute", "10000/hour"],
    headers_enabled=True,
    storage_uri=settings.REDIS_URL,
    in_memory_fallback_enabled=True,
    enabled=settings.RATELIMIT_ENABLED,
    key_style="endpoint",
)
