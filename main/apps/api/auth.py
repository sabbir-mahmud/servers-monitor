"""Bearer-token authentication for /api/v1/ and /metrics.

Token auth only: these views never look at the session cookie, so they're CSRF-exempt
without letting a malicious page ride a signed-in browser. A token acts as its owner,
limited by its scope (read, or read + run actions) and by the owner's role.
"""

import functools
import json
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.core.utils import client_ip
from apps.users import throttle

from .models import ApiToken, hash_token


def error(status: int, message: str, **headers) -> JsonResponse:
    response = JsonResponse({"error": message}, status=status)
    for name, value in headers.items():
        response[name.replace("_", "-")] = value
    return response


def _authenticate(request):
    """(token, None) or (None, error response)."""
    ip = client_ip(request) or "unknown"
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None, error(401, "Send your API token: Authorization: Bearer smt_…",
                           WWW_Authenticate='Bearer realm="api"')  # fmt: skip
    token = (
        ApiToken.objects.select_related("owner")
        .filter(token_hash=hash_token(header[7:].strip()))
        .first()
    )
    if token is None or not token.is_active or not token.owner.is_active:
        # Guessing tokens is hopeless (256 bits), but don't let anyone hammer the lookup.
        if not throttle.allow("api-bad-token", ip, 30, 60):
            return None, error(429, "Too many bad tokens. Wait a minute.", Retry_After="60")
        return None, error(401, "This token is invalid, revoked or expired.",
                           WWW_Authenticate='Bearer error="invalid_token"')  # fmt: skip
    if not throttle.allow("api", str(token.pk), settings.API_RATE_LIMIT, 60):
        return None, error(429, f"Rate limit: {settings.API_RATE_LIMIT} requests a minute per token.",
                           Retry_After="60")  # fmt: skip
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at > timedelta(minutes=1):
        ApiToken.objects.filter(pk=token.pk).update(
            last_used_at=now, last_used_ip=client_ip(request)
        )
    return token, None


def api_view(*, perm: str, methods=("GET",), write_perm: str | None = None):
    """Authenticate, check the method, the owner's permission and (for writes) the token's
    scope and `write_perm`. The view gets `request.user` = the owner and `request.api_token`."""

    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, f"Use {' or '.join(methods)}.", Allow=", ".join(methods))
            token, failure = _authenticate(request)
            if failure is not None:
                return failure
            request.user, request.api_token = token.owner, token
            if not token.owner.has_perm(perm):
                return error(403, "Your role doesn't allow this.")
            if request.method != "GET":
                if not token.can_operate:
                    return error(403, "This token is read-only.")
                if write_perm and not token.owner.has_perm(write_perm):
                    return error(403, "Your role doesn't allow this.")
                try:
                    request.json = json.loads(request.body or b"{}")
                except ValueError:
                    return error(400, "The body must be JSON.")
                if not isinstance(request.json, dict):
                    return error(400, "The body must be a JSON object.")
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
