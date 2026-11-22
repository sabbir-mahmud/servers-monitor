from django.urls import path

from . import views

app_name = "devices"
urlpatterns = [
    path("new/", views.DeviceCreateView.as_view(), name="create"),
    path("places/", views.PlaceListView.as_view(), name="places"),
    path("places/new/", views.PlaceCreateView.as_view(), name="place_create"),
    path("places/<uuid:pk>/", views.PlaceUpdateView.as_view(), name="place_update"),
    path("places/<uuid:pk>/delete/", views.PlaceDeleteView.as_view(), name="place_delete"),
    path("<uuid:pk>/", views.DeviceDetailView.as_view(), name="detail"),
    path("<uuid:pk>/pair/", views.DevicePairView.as_view(), name="pair"),
    path("<uuid:pk>/edit/", views.DeviceUpdateView.as_view(), name="update"),
    path("<uuid:pk>/controls/", views.DeviceControlsView.as_view(), name="controls"),
    path("<uuid:pk>/beep/", views.DeviceBeepView.as_view(), name="beep"),
    path("<uuid:pk>/delete/", views.DeviceDeleteView.as_view(), name="delete"),
]
