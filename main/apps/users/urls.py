from django.urls import path

from . import views

app_name = "users"
urlpatterns = [
    path("", views.UserListView.as_view(), name="list"),
    path("invite/", views.InviteView.as_view(), name="invite"),
    path("invite/<uuid:pk>/revoke/", views.InviteRevokeView.as_view(), name="invite_revoke"),
    path("<int:pk>/", views.UserDetailView.as_view(), name="detail"),
]
