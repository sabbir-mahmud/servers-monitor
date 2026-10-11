from django.urls import path

from . import views

app_name = "monitors"
urlpatterns = [
    path("", views.MonitorListView.as_view(), name="list"),
    path("new/", views.MonitorCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.MonitorDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.MonitorUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.MonitorDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/check/", views.MonitorCheckNowView.as_view(), name="check"),
    path("<uuid:pk>/toggle/", views.MonitorToggleView.as_view(), name="toggle"),
]
