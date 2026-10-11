from django.urls import path

from . import views

app_name = "status"

urlpatterns = [
    path("", views.StatusPageListView.as_view(), name="list"),
    path("new/", views.StatusPageCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.StatusPageDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.StatusPageUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.StatusPageDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/preview/", views.StatusPagePreviewView.as_view(), name="preview"),
    path("<uuid:pk>/download/", views.StatusPageDownloadView.as_view(), name="download"),
    path("<uuid:pk>/publish/", views.StatusPagePublishView.as_view(), name="publish"),
    path("<uuid:pk>/incidents/", views.IncidentCreateView.as_view(), name="incident_create"),
    path(
        "<uuid:pk>/incidents/<int:incident_id>/",
        views.IncidentUpdateView.as_view(),
        name="incident_update",
    ),
]
