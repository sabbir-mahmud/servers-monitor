from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from .models import Membership
from .permissions import bind

SESSION_KEY = "account_id"

# Reachable by a signed-in user who belongs to no account yet: create one, accept an
# invitation, manage their own login, sign out.
NO_ACCOUNT_OK = {
    "tenants:create",
    "account",
    "account_2fa",
    "logout",
    "login",
    "login_verify",
    "accept_invite",
    "password_forgot",
    "password_reset_confirm",
    "signup",
    "manifest",
    "service_worker",
    "offline",
}
NO_ACCOUNT_PREFIXES = ("/static/", "/oidc/", "/api/", "/device/", "/metrics")


def memberships(user):
    return list(
        Membership.objects.filter(user=user).select_related("account").order_by("created_at")
    )


def choose(request, user) -> tuple[Membership | None, list[Membership]]:
    """The membership the session points at, else the user's first one."""
    rows = memberships(user)
    wanted = request.session.get(SESSION_KEY)
    for m in rows:
        if str(m.account_id) == wanted:
            return m, rows
    current = rows[0] if rows else None
    if current is not None:
        request.session[SESSION_KEY] = str(current.account_id)
    return current, rows


class CurrentAccountMiddleware:
    """Sets request.account / request.membership and binds the role's permissions to
    request.user. Must sit after AuthenticationMiddleware and before anything that
    checks permissions (Require2FAMiddleware)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.account = request.membership = None
        request.memberships = []
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            membership, request.memberships = choose(request, user)
            bind(user, membership)
            if membership is not None:
                request.membership, request.account = membership, membership.account
            elif not self._allowed_without_account(request):
                target = reverse("tenants:create")
                if getattr(request, "htmx", False):
                    response = HttpResponse(status=204)
                    response["HX-Redirect"] = target
                    return response
                return redirect(target)
        return self.get_response(request)

    @staticmethod
    def _allowed_without_account(request) -> bool:
        from django.conf import settings

        path = request.path_info
        if path.startswith(NO_ACCOUNT_PREFIXES) or path.startswith("/" + settings.ADMIN_URL):
            return True
        try:
            match = resolve(path)
        except Resolver404:
            return True  # a 404 is a 404, with or without an account
        return match.view_name in NO_ACCOUNT_OK
