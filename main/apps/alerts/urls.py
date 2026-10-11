from django.urls import path

from . import views

app_name = "alerts"
urlpatterns = [
    path("", views.AlertListView.as_view(), name="list"),
    path("<uuid:pk>/acknowledge/", views.AlertAcknowledgeView.as_view(), name="acknowledge"),
]
