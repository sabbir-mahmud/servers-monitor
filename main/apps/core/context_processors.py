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
    account = getattr(request, "account", None)
    ctx["ACCOUNT"] = account
    ctx["MEMBERSHIP"] = getattr(request, "membership", None)
    ctx["MEMBERSHIPS"] = getattr(request, "memberships", [])
    ctx["NAV"] = _nav_counts(user, account) if account is not None else {}
    ctx["ALLOW_ACCOUNT_CREATE"] = settings.ALLOW_SIGNUP or user.is_superuser
    return ctx


def _insecure_transport(request, user) -> bool:
    """Plain HTTP from another machine: the session cookie is readable on the network."""
    import ipaddress

    if request.is_secure() or not (user.is_superuser or user.has_perm("users.view_user")):
        return False
    try:
        return not ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
    except ValueError:
        return True


def _nav_counts(user, account) -> dict:
    """Sidebar badges: a few COUNT queries so problems are visible from every page."""
    from apps.alerts.models import Alert
    from apps.fleet.models import Health, Server
    from apps.monitors.models import Monitor, MonitorStatus

    counts = {}
    if user.has_perm("fleet.view_server"):
        servers = Server.objects.filter(account=account)
        counts["servers"] = servers.count()
        counts["servers_offline"] = servers.filter(
            is_enabled=True, last_health=Health.OFFLINE
        ).count()
    if user.has_perm("monitors.view_monitor"):
        monitors = Monitor.objects.filter(account=account)
        counts["apps"] = monitors.count()
        counts["apps_down"] = monitors.filter(is_enabled=True, status=MonitorStatus.DOWN).count()
    if user.has_perm("alerts.view_alert"):
        counts["alerts"] = Alert.objects.filter(account=account, resolved_at__isnull=True).count()
    if user.has_perm("tenants.view_person"):
        counts["people"] = account.people.count()
    if user.is_superuser:
        # A dot on System when nobody would be told the dashboard died.
        counts["system_problem"] = not settings.WATCHDOG_URL
    if user.has_perm("alerts.view_maintenancewindow"):
        from django.utils import timezone

        from apps.alerts.models import MaintenanceWindow

        now = timezone.now()
        counts["maintenance"] = MaintenanceWindow.objects.filter(
            account=account, starts_at__lte=now, ends_at__gt=now
        ).count()
    return counts
