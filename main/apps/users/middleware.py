from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

from . import sso, twofactor

# Reachable while 2FA setup is still owed: the setup page itself and signing out.
EXEMPT_NAMES = {"account_2fa", "logout", "login", "login_verify"}


class Require2FAMiddleware:
    """Admins without a confirmed authenticator are sent to set one up before anything else."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith("/static/")
            and twofactor.is_required(user)
            and not sso.is_sso_session(request)  # the provider handles 2FA for SSO
            and not twofactor.is_enabled(user)
            and not self._exempt(request)
        ):
            target = reverse("account_2fa")
            if getattr(request, "htmx", False):
                response = HttpResponse(status=204)
                response["HX-Redirect"] = target
                return response
            return redirect(target)
        return self.get_response(request)

    @staticmethod
    def _exempt(request) -> bool:
        try:
            return resolve(request.path_info).url_name in EXEMPT_NAMES
        except Resolver404:
            return False
