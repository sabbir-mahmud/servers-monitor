from django.urls import path

from . import views

app_name = "fleet"
urlpatterns = [
    path("devices/", views.DashboardView.as_view(), name="dashboard"),
    path("devices/servers/new/", views.ServerCreateView.as_view(), name="server_create"),
    path("devices/servers/<uuid:pk>/", views.ServerDetailView.as_view(), name="server_detail"),
    path("devices/servers/<uuid:pk>/edit/", views.ServerUpdateView.as_view(), name="server_update"),
    path(
        "devices/servers/<uuid:pk>/delete/", views.ServerDeleteView.as_view(), name="server_delete"
    ),
    path("devices/servers/upgrade/", views.AgentUpgradeView.as_view(), name="upgrade_all"),
    path(
        "devices/servers/<uuid:pk>/upgrade/",
        views.AgentUpgradeView.as_view(),
        name="server_upgrade",
    ),
    path(
        "devices/servers/<uuid:pk>/enroll/", views.ServerEnrollView.as_view(), name="server_enroll"
    ),
    path(
        "devices/servers/<uuid:pk>/rotate-token/",
        views.ServerRotateTokenView.as_view(),
        name="server_rotate_token",
    ),
]
