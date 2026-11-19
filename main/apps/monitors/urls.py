from django.urls import path

from . import views

app_name = "monitors"
urlpatterns = [
    path("", views.MonitorListView.as_view(), name="list"),
    path("new/", views.MonitorCreateView.as_view(), name="create"),
    path("status-pages/", views.StatusPageListView.as_view(), name="status_pages"),
    path("status-pages/new/", views.StatusPageCreateView.as_view(), name="status_page_create"),
    path(
        "status-pages/<uuid:pk>/", views.StatusPageUpdateView.as_view(), name="status_page_update"
    ),
    path(
        "status-pages/<uuid:pk>/delete/",
        views.StatusPageDeleteView.as_view(),
        name="status_page_delete",
    ),
    path("<uuid:pk>/", views.MonitorDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.MonitorUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.MonitorDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/check/", views.MonitorCheckNowView.as_view(), name="check"),
    path("<uuid:pk>/toggle/", views.MonitorToggleView.as_view(), name="toggle"),
]
