"""Login brute-force throttle keyed on (username, IP), backed by the shared cache."""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(username: str, ip: str | None) -> str:
    digest = hashlib.sha256(f"{username.lower()}|{ip}".encode()).hexdigest()[:32]
    return f"login-fail:{digest}"


def is_locked(username: str, ip: str | None) -> bool:
    return cache.get(_key(username, ip), 0) >= settings.LOGIN_FAILURE_LIMIT


def record_failure(username: str, ip: str | None) -> None:
    key = _key(username, ip)
    if cache.add(key, 1, settings.LOGIN_FAILURE_TIMEOUT):
        return
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, settings.LOGIN_FAILURE_TIMEOUT)


def clear(username: str, ip: str | None) -> None:
    cache.delete(_key(username, ip))


def allow(scope: str, key: str, limit: int, window: int) -> bool:
    """Fixed-window rate limit: True if this hit is within `limit` per `window` seconds.
    Used for password resets, sign-ups and invitation links (per IP and per address)."""
    digest = hashlib.sha256(f"{scope}|{key.lower()}".encode()).hexdigest()[:32]
    cache_key = f"rate:{scope}:{digest}"
    if cache.add(cache_key, 1, window):
        return True
    try:
        return cache.incr(cache_key) <= limit
    except ValueError:  # expired between add and incr
        cache.set(cache_key, 1, window)
        return True
