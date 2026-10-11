from django.urls import path

from . import views

app_name = "operations"
urlpatterns = [
    path("", views.JobListView.as_view(), name="job_list"),
    path("new/", views.JobCreateView.as_view(), name="job_create"),
    path("<uuid:pk>/", views.JobDetailView.as_view(), name="job_detail"),
    path("commands/<uuid:pk>/", views.CommandDetailView.as_view(), name="command_detail"),
    path("commands/<uuid:pk>/cancel/", views.CommandCancelView.as_view(), name="command_cancel"),
]
