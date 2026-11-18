from django.urls import path

from . import api, views

app_name = "devices"
urlpatterns = [
    path("new/", views.DeviceCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.DeviceDetailView.as_view(), name="detail"),
    path("<uuid:pk>/pair/", views.DevicePairView.as_view(), name="pair"),
    path("<uuid:pk>/edit/", views.DeviceUpdateView.as_view(), name="update"),
    path("<uuid:pk>/controls/", views.DeviceControlsView.as_view(), name="controls"),
    path("<uuid:pk>/delete/", views.DeviceDeleteView.as_view(), name="delete"),
]

# The device apps' API (token auth, no session): mounted at /device/v1/.
api_urlpatterns = [
    path("pair", api.pair, name="device_api_pair"),
    path("checkin", api.checkin, name="device_api_checkin"),
    path("blocklist", api.blocklist_view, name="device_api_blocklist"),
]
