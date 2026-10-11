import logging

from apps.core.utils import client_ip

from .models import AuditLog

logger = logging.getLogger(__name__)


def record(request_or_user, action: str, target: str = "", server=None, **data) -> AuditLog:
    """Write one audit row. Accepts a request (actor + IP) or a bare user (Celery, commands)."""
    request = request_or_user if hasattr(request_or_user, "META") else None
    user = getattr(request, "user", None) if request is not None else request_or_user
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    entry = AuditLog.objects.create(
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
