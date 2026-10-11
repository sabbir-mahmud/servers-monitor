from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.users import views as user_views

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
