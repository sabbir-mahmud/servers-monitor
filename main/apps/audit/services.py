import logging

from apps.core.utils import client_ip

from .models import AuditLog

logger = logging.getLogger(__name__)


def record(
    request_or_user, action: str, target: str = "", server=None, account=None, **data
) -> AuditLog:
    """Write one audit row. Accepts a request (actor + IP) or a bare user (Celery, commands).
    The row belongs to `account`, else the request's current account, else the server's;
    sign-ins and other events outside any account have none (superusers see those)."""
    request = request_or_user if hasattr(request_or_user, "META") else None
    user = getattr(request, "user", None) if request is not None else request_or_user
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    if token := getattr(request, "api_token", None):
        data.setdefault("via", f"API token {token.name} ({token.prefix}…)")
    if account is None:
        account = getattr(request, "account", None)
    account_id = getattr(account, "pk", account)
    if account_id is None and server is not None:
        account_id = server.account_id
    entry = AuditLog.objects.create(
        account_id=account_id,
        actor=user,
        actor_label=user.get_username() if user else "",
        action=action,
        target=str(target)[:200],
        server=server,
        data=data,
        ip=client_ip(request) if request is not None else None,
    )
    logger.info("audit %s by %s on %s", action, entry.actor_label or "system", entry.target)
    return entry
