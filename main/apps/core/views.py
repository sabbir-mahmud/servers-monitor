from django.conf import settings
from django.db.models import Count
from django.views.generic import TemplateView

from .mixins import StaffViewMixin
from .models import SystemState


def _size(path) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += (path.parent / (path.name + suffix)).stat().st_size
        except OSError:
            pass
    return total


class SystemView(StaffViewMixin, TemplateView):
    """The dashboard's own health: is it watched, backed up, affordable and current?"""

    permission_required = "users.view_user"
    template_name = "core/system.html"

    def get_context_data(self, **kwargs):
        from apps.agent_api import relay
        from apps.fleet.models import Server

        ctx = super().get_context_data(**kwargs)
        servers = Server.objects.all()
        versions = list(
            servers.exclude(agent_version="")
            .values("agent_version")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
        ctx.update(
            relay_alive=relay.is_alive(),
            relay_last_alive=relay.last_alive_at(),
            watchdog=SystemState.get("watchdog"),
            watchdog_configured=bool(settings.WATCHDOG_URL),
            downtime=SystemState.get("last_downtime"),
            backup=_backup_state(),
            usage=_usage(),
            versions=versions,
            outdated=sum(s.agent_outdated for s in servers.only("agent_version")),
            unsigned=servers.filter(signing_salt="").count(),
            server_count=servers.count(),
            db_size=_size(settings.SQLITE_PATH),
            settings_summary=[
                (
                    "Email",
                    "configured" if settings.EMAIL_ENABLED else "off (Admins hand out links)",
                ),
                ("HTTPS", "on" if settings.HTTPS else "off (plain HTTP)"),
                (
                    "Public sign-up",
                    "on, with approval" if settings.ALLOW_SIGNUP else "off (invite-only)",
                ),
                (
                    "Private check targets",
                    "allowed" if settings.MONITOR_ALLOW_PRIVATE_TARGETS else "blocked",
                ),
                ("Agent source", f"{settings.AGENT_REPO} @ {settings.AGENT_REF}"),
                ("Time zone", settings.TIME_ZONE),
            ],
        )
        return ctx


def _usage():
    try:
        from apps.agent_api.usage import month_summary
    except ImportError:  # usage meter not installed yet
        return None
    return month_summary()


def _backup_state():
    from .backup import is_stale

    state = SystemState.get("backup")
    if state:
        state = {**state, "stale": is_stale(state)}
    return state
