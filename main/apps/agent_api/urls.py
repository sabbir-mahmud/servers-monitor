from django.urls import path

from . import views

app_name = "agent_api"
urlpatterns = [
    path("heartbeat/", views.heartbeat, name="heartbeat"),
    path("commands/", views.poll_commands, name="commands"),
    path("commands/<uuid:pk>/", views.command_result, name="command_result"),
]
