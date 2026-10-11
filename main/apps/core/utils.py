from django.conf import settings


def client_ip(request) -> str | None:
    """Caller's IP. X-Forwarded-For is only honoured behind a proxy that overwrites it."""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None
