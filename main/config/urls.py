from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpResponsePermanentRedirect
from django.urls import include, path, re_path, reverse

from apps.api import ui as api_ui
from apps.api import views as api_views
from apps.core import pwa
from apps.core import views as core_views
from apps.devices import connect as device_connect
from apps.devices.urls import api_urlpatterns as device_api_urls
from apps.tenants import views as tenant_views
from apps.users import views as user_views


def _admin_login(request, extra_context=None):
    """Django admin's own login page has no brute-force throttle, and it guards the most
    powerful accounts. Send it through the dashboard's throttled login instead."""
    if request.user.is_authenticated:
        raise PermissionDenied  # signed in but not staff: no redirect loop
    return redirect_to_login(
        request.GET.get("next") or reverse("admin:index"), login_url=reverse("login")
    )


admin.site.login = _admin_login


def legacy_server_url(request, rest):
    """/servers/ → /devices/, /servers/<id>/… → /devices/servers/<id>/… (query kept).
    Always a path on this site, so it can't be turned into an open redirect."""
    target = reverse("fleet:dashboard") + (f"servers/{rest}" if rest else "")
    if request.META.get("QUERY_STRING"):
        target += "?" + request.META["QUERY_STRING"]
    return HttpResponsePermanentRedirect(target)


urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("login/", user_views.ThrottledLoginView.as_view(), name="login"),
    path("login/verify/", user_views.LoginVerifyView.as_view(), name="login_verify"),
    path("account/2fa/", user_views.TwoFactorView.as_view(), name="account_2fa"),
    path("logout/", user_views.LogoutView.as_view(), name="logout"),
    path("password/forgot/", user_views.ForgotPasswordView.as_view(), name="password_forgot"),
    path(
        "password/reset/<uidb64>/<token>/",
        user_views.ResetPasswordView.as_view(),
        name="password_reset_confirm",
    ),
    path("invite/<str:token>/", user_views.AcceptInviteView.as_view(), name="accept_invite"),
    path("signup/", user_views.SignupView.as_view(), name="signup"),
    path("account/", user_views.AccountView.as_view(), name="account"),
    path("account/tokens/", api_ui.TokenListView.as_view(), name="api_tokens"),
    path(
        "account/tokens/<uuid:pk>/revoke/",
        api_ui.TokenRevokeView.as_view(),
        name="api_token_revoke",
    ),
    path("api/v1/", include("apps.api.urls")),
    path("device/v1/", include(device_api_urls)),
    # Connect a computer: the link a parent sends, and the scripts it tells you to run.
    path(
        "d/uninstall.sh",
        device_connect.uninstall_script,
        {"kind": "sh"},
        name="device_uninstall_sh",
    ),
    path(
        "d/uninstall.ps1",
        device_connect.uninstall_script,
        {"kind": "ps1"},
        name="device_uninstall_ps1",
    ),
    path("d/agent/<str:name>", device_connect.agent_file, name="device_agent_file"),
    path("d/app/NexGuard.apk", device_connect.android_app, name="phone_app_android"),
    path("d/<str:code>/", device_connect.page, name="device_connect"),
    path(
        "d/<str:code>/install.sh",
        device_connect.install_script,
        {"kind": "sh"},
        name="device_install_sh",
    ),
    path(
        "d/<str:code>/install.ps1",
        device_connect.install_script,
        {"kind": "ps1"},
        name="device_install_ps1",
    ),
    path("devices/", include("apps.devices.urls")),
    path("metrics", api_views.metrics, name="metrics"),
    path("manifest.webmanifest", pwa.manifest, name="manifest"),
    path("sw.js", pwa.service_worker, name="service_worker"),
    path("offline/", pwa.offline, name="offline"),
    path("users/", include("apps.users.urls")),
    path("system/", core_views.SystemView.as_view(), name="system"),
    path("jobs/", include("apps.operations.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("apps/", include("apps.monitors.urls")),
    path("audit/", include("apps.audit.urls")),
    path("", tenant_views.HomeView.as_view(), name="home"),
    path("", include("apps.tenants.urls")),
    path("", include("apps.fleet.urls")),
    # Before 3.0 the server pages lived under /servers/: keep old bookmarks and the links
    # in alert emails already sent working.
    re_path(r"^servers/(?P<rest>.*)$", legacy_server_url),
]
if settings.OIDC_ENABLED:  # its views read the provider's endpoints when they're created
    urlpatterns.insert(0, path("oidc/", include("mozilla_django_oidc.urls")))
