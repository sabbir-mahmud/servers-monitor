from django.urls import path

from . import ssh_views, views

app_name = "operations"
urlpatterns = [
    path("", views.JobListView.as_view(), name="job_list"),
    path("new/", views.JobCreateView.as_view(), name="job_create"),
    path("ssh/", ssh_views.SshPageView.as_view(), name="ssh"),
    path("ssh/open/", ssh_views.SshOpenView.as_view(), name="ssh_open"),
    path("ssh/s/<str:sid>/read/", ssh_views.SshReadView.as_view(), name="ssh_read"),
    path("ssh/s/<str:sid>/write/", ssh_views.SshWriteView.as_view(), name="ssh_write"),
    path("ssh/s/<str:sid>/close/", ssh_views.SshCloseView.as_view(), name="ssh_close"),
    path("ssh/keys/<int:pk>/forget/", ssh_views.SshForgetKeyView.as_view(), name="ssh_forget_key"),
    path("<uuid:pk>/", views.JobDetailView.as_view(), name="job_detail"),
    path("<uuid:pk>/<str:op>/", views.JobControlView.as_view(), name="job_control"),
    path("schedules/", views.ScheduleListView.as_view(), name="schedule_list"),
    path("schedules/new/", views.ScheduleCreateView.as_view(), name="schedule_create"),
    path("schedules/<uuid:pk>/", views.ScheduleUpdateView.as_view(), name="schedule_update"),
    path("schedules/<uuid:pk>/delete/", views.ScheduleDeleteView.as_view(), name="schedule_delete"),
    path("commands/<uuid:pk>/", views.CommandDetailView.as_view(), name="command_detail"),
    path("commands/<uuid:pk>/cancel/", views.CommandCancelView.as_view(), name="command_cancel"),
    path("commands/<uuid:pk>/stop/", views.CommandStopView.as_view(), name="command_stop"),
]
