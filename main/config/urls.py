from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.users import views as user_views

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path("login/", user_views.ThrottledLoginView.as_view(), name="login"),
    path("logout/", user_views.LogoutView.as_view(), name="logout"),
    path("api/agent/v1/", include("apps.agent_api.urls")),
    path("jobs/", include("apps.operations.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("audit/", include("apps.audit.urls")),
    path("", include("apps.fleet.urls")),
]
