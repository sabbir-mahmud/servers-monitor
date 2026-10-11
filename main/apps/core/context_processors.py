from django.conf import settings


def site(request):
    ctx = {"SITE_NAME": settings.SITE_NAME}
    user = getattr(request, "user", None)
    # Full page loads only: HTMX partial refreshes never render the sidebar.
    if user is None or not user.is_authenticated or getattr(request, "htmx", False):
        return ctx
    from apps.agent_api import relay

    # Without the relay process, agents' heartbeats and job output pile up in Redis unread.
    ctx["RELAY_DOWN"] = not relay.is_alive()
    ctx["NAV"] = _nav_counts(user)
    return ctx


def _nav_counts(user) -> dict:
    """Sidebar badges: a few COUNT queries so problems are visible from every page."""
    from apps.alerts.models import Alert
    from apps.fleet.models import Health, Server
    from apps.monitors.models import Monitor, MonitorStatus

    counts = {}
    if user.has_perm("fleet.view_server"):
        counts["servers"] = Server.objects.count()
        counts["servers_offline"] = Server.objects.filter(
            is_enabled=True, last_health=Health.OFFLINE
        ).count()
    if user.has_perm("monitors.view_monitor"):
        counts["apps"] = Monitor.objects.count()
        counts["apps_down"] = Monitor.objects.filter(
            is_enabled=True, status=MonitorStatus.DOWN
        ).count()
    if user.has_perm("alerts.view_alert"):
        counts["alerts"] = Alert.objects.filter(resolved_at__isnull=True).count()
    return counts
