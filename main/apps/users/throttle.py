"""Login brute-force throttle and a general rate limiter, backed by the cache.

Failed sign-ins are counted three ways, and any one of them locks the next attempt:
  - per account and IP     LOGIN_FAILURE_LIMIT          (a person mistyping)
  - per account, any IP    LOGIN_ACCOUNT_FAILURE_LIMIT  (one account, many IPs)
  - per IP, any account    LOGIN_IP_FAILURE_LIMIT       (password spraying)
"Account" is resolved from whatever was typed, so a username and its email address share
one counter instead of doubling the attempts.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q


def _digest(*parts) -> str:
    return hashlib.sha256("|".join(str(p).lower() for p in parts).encode()).hexdigest()[:32]


def account_key(identifier: str) -> str:
    from django.contrib.auth import get_user_model

    identifier = (identifier or "").strip()
    lookup = Q(username__iexact=identifier)
    if "@" in identifier:
        lookup |= Q(email__iexact=identifier)
    pk = get_user_model().objects.filter(lookup).values_list("pk", flat=True).first()
    return f"user:{pk}" if pk is not None else f"name:{identifier.lower()}"


def _keys(identifier: str, ip: str | None) -> list[tuple[str, int]]:
    account = account_key(identifier)
    return [
        (f"login-fail:pair:{_digest(account, ip)}", settings.LOGIN_FAILURE_LIMIT),
        (f"login-fail:acct:{_digest(account)}", settings.LOGIN_ACCOUNT_FAILURE_LIMIT),
        (f"login-fail:ip:{_digest(ip)}", settings.LOGIN_IP_FAILURE_LIMIT),
    ]


def is_locked(identifier: str, ip: str | None) -> bool:
    return any(cache.get(key, 0) >= limit for key, limit in _keys(identifier, ip))


def _incr(key: str) -> None:
    if cache.add(key, 1, settings.LOGIN_FAILURE_TIMEOUT):
        return
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, settings.LOGIN_FAILURE_TIMEOUT)


def record_failure(identifier: str, ip: str | None) -> None:
    for key, _ in _keys(identifier, ip):
        _incr(key)


def clear(identifier: str, ip: str | None) -> None:
    """After a successful sign-in: forget this account's failures (not the IP's)."""
    pair, account, _ip = _keys(identifier, ip)
    cache.delete_many([pair[0], account[0]])


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
