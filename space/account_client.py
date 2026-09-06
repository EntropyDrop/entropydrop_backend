"""Narrow authenticated RPC to cloud accounts; never connects to the cloud database."""
import hashlib
import json
import time
import httpx
from fastapi import HTTPException
from redis import Redis
from config import settings

_cache = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)


def call(path, payload):
    base = settings.SPACE_ACCOUNT_API_URL.rstrip("/")
    if not base or not settings.SPACE_ACCOUNT_SERVICE_TOKEN:
        raise HTTPException(503, detail={"code": "ACCOUNT_SERVICE_NOT_CONFIGURED"})
    if not base.startswith("https://") and not base.startswith("http://127.0.0.1:"):
        raise HTTPException(503, detail={"code": "ACCOUNT_SERVICE_TLS_REQUIRED"})
    try:
        response = httpx.post(base + "/internal/space/" + path, json=payload,
            headers={"X-Space-Service-Token": settings.SPACE_ACCOUNT_SERVICE_TOKEN},
            timeout=httpx.Timeout(8, connect=3), follow_redirects=False, trust_env=False)
        if not response.is_success:
            detail = response.json().get("detail", {"code": "ACCOUNT_SERVICE_ERROR"})
            raise HTTPException(response.status_code, detail=detail)
        return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail={"code": "ACCOUNT_SERVICE_UNAVAILABLE"}) from None


def identity(credential):
    key = "space:identity:" + hashlib.sha256(credential.encode()).hexdigest()
    try:
        cached = _cache.get(key)
        if cached:
            data = json.loads(cached)
            if data["expires_at"] > time.time():
                return data
    except Exception:
        pass
    data = call("identity", {"credential": credential})
    ttl = min(max(1, settings.SPACE_IDENTITY_CACHE_SECONDS), 30,
              int(data["expires_at"] - time.time()))
    if ttl > 0:
        try:
            _cache.set(key, json.dumps(data), ex=ttl)
        except Exception:
            pass
    return data
