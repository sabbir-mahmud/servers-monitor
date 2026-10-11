from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.urls import include, path, reverse

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

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("login/", user_views.ThrottledLoginView.as_view(), name="login"),
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
    path("users/", include("apps.users.urls")),
    path("jobs/", include("apps.operations.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("apps/", include("apps.monitors.urls")),
    path("audit/", include("apps.audit.urls")),
    path("", include("apps.fleet.urls")),
]
