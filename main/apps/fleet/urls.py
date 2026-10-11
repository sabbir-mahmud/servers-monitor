from django.urls import path

from . import views

app_name = "fleet"
urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("servers/new/", views.ServerCreateView.as_view(), name="server_create"),
    path("servers/<uuid:pk>/", views.ServerDetailView.as_view(), name="server_detail"),
    path("servers/<uuid:pk>/edit/", views.ServerUpdateView.as_view(), name="server_update"),
    path("servers/<uuid:pk>/delete/", views.ServerDeleteView.as_view(), name="server_delete"),
    path("servers/upgrade/", views.AgentUpgradeView.as_view(), name="upgrade_all"),
    path("servers/<uuid:pk>/upgrade/", views.AgentUpgradeView.as_view(), name="server_upgrade"),
    path("servers/<uuid:pk>/enroll/", views.ServerEnrollView.as_view(), name="server_enroll"),
    path(
        "servers/<uuid:pk>/rotate-token/",
        views.ServerRotateTokenView.as_view(),
        name="server_rotate_token",
    ),
]
