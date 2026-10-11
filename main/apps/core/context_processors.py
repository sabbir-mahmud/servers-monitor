from django.conf import settings


def site(request):
    ctx = {"SITE_NAME": settings.SITE_NAME}
    user = getattr(request, "user", None)
    # Without the relay process, agents' heartbeats and job output pile up in Redis unread.
    if user is not None and user.is_authenticated and not getattr(request, "htmx", False):
        from apps.agent_api import relay

        ctx["RELAY_DOWN"] = not relay.is_alive()
    return ctx
