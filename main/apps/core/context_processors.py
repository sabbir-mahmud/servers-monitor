from django.conf import settings

# Pages that use the sign-in layout even for a signed-in visitor (opening an invite or a
# reset link while logged in must not render an empty dashboard).
AUTH_PAGES = {
    "login",
    "login_verify",
    "password_forgot",
    "password_reset_confirm",
    "accept_invite",
    "signup",
}


def site(request):
    match = getattr(request, "resolver_match", None)
    ctx = {
        "SITE_NAME": settings.SITE_NAME,
        "APP_VERSION": settings.APP_VERSION,
        "TIME_ZONE_NAME": settings.TIME_ZONE,
        "AUTH_PAGE": bool(match and match.url_name in AUTH_PAGES and not match.namespace),
        "INVITATION_DAYS": settings.INVITATION_DAYS,
        "PASSWORD_RESET_HOURS": settings.PASSWORD_RESET_TIMEOUT // 3600,
        "SSO_ENABLED": settings.OIDC_ENABLED,
        "SSO_NAME": settings.OIDC_PROVIDER_NAME,
    }
    user = getattr(request, "user", None)
    # Full page loads only: HTMX partial refreshes never render the sidebar.
    if user is None or not user.is_authenticated or getattr(request, "htmx", False):
        return ctx
    from apps.agent_api import relay

    # Without the relay process, agents' heartbeats and job output pile up in Redis unread.
    ctx["RELAY_DOWN"] = not relay.is_alive()
    ctx["INSECURE_TRANSPORT"] = _insecure_transport(request, user)
    ctx["NAV"] = _nav_counts(user)
    return ctx


def _insecure_transport(request, user) -> bool:
    """Plain HTTP from another machine: the session cookie is readable on the network."""
    import ipaddress

    if request.is_secure() or not user.has_perm("users.view_user"):
        return False
    try:
        return not ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
    except ValueError:
        return True


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
    if user.has_perm("users.view_user"):
        from apps.users.models import User

        counts["pending_users"] = User.objects.filter(pending_approval=True).count()
        # A dot on System when nobody would be told the dashboard died.
        counts["system_problem"] = not settings.WATCHDOG_URL
    if user.has_perm("alerts.view_maintenancewindow"):
        from django.utils import timezone

        from apps.alerts.models import MaintenanceWindow

        now = timezone.now()
        counts["maintenance"] = MaintenanceWindow.objects.filter(
            starts_at__lte=now, ends_at__gt=now
        ).count()
    return counts
