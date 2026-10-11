from django.urls import path

from . import views

app_name = "alerts"
urlpatterns = [
    path("", views.AlertListView.as_view(), name="list"),
    path("<uuid:pk>/acknowledge/", views.AlertAcknowledgeView.as_view(), name="acknowledge"),
    path("notifications/", views.ChannelListView.as_view(), name="channels"),
    path("notifications/new/", views.ChannelCreateView.as_view(), name="channel_create"),
    path("notifications/<uuid:pk>/edit/", views.ChannelUpdateView.as_view(), name="channel_update"),
    path(
        "notifications/<uuid:pk>/delete/", views.ChannelDeleteView.as_view(), name="channel_delete"
    ),
    path("notifications/<uuid:pk>/test/", views.ChannelTestView.as_view(), name="channel_test"),
    path("notifications/telegram-chats/", views.TelegramChatsView.as_view(), name="telegram_chats"),
    path("maintenance/", views.MaintenanceListView.as_view(), name="maintenance"),
    path("maintenance/new/", views.MaintenanceCreateView.as_view(), name="maintenance_create"),
    path("maintenance/mute/", views.QuickMuteView.as_view(), name="quick_mute"),
    path("maintenance/<uuid:pk>/end/", views.MaintenanceEndView.as_view(), name="maintenance_end"),
    path(
        "maintenance/<uuid:pk>/delete/",
        views.MaintenanceDeleteView.as_view(),
        name="maintenance_delete",
    ),
]
