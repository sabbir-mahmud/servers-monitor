from django.urls import path

from . import views

urlpatterns = [
    path("servers/", views.servers, name="api_servers"),
    path("servers/<uuid:pk>/", views.server_detail, name="api_server"),
    path("monitors/", views.monitors, name="api_monitors"),
    path("monitors/<uuid:pk>/", views.monitor_detail, name="api_monitor"),
    path("alerts/", views.alerts, name="api_alerts"),
    path("jobs/", views.jobs, name="api_jobs"),
    path("jobs/<uuid:pk>/", views.job_detail, name="api_job"),
    path("commands/<uuid:pk>/", views.command_detail, name="api_command"),
]
