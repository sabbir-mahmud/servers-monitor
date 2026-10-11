from django.conf import settings


def client_ip(request) -> str | None:
    """Caller's IP. X-Forwarded-For is only honoured behind a proxy that overwrites it."""
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def safe_next(request, fallback: str) -> str:
    """The `next` the form posted, if it points back into this site; otherwise `fallback`.
    Never redirect to an address taken straight from the request (open redirect)."""
    from django.utils.http import url_has_allowed_host_and_scheme

    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return fallback
