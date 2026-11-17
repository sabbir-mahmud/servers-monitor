from django.urls import path

from . import views

app_name = "tenants"
urlpatterns = [
    path("accounts/new/", views.AccountCreateView.as_view(), name="create"),
    path("accounts/switch/", views.SwitchAccountView.as_view(), name="switch"),
    path("settings/account/", views.AccountSettingsView.as_view(), name="settings"),
    path("people/", views.PersonListView.as_view(), name="people"),
    path("people/new/", views.PersonCreateView.as_view(), name="person_create"),
    path("people/<uuid:pk>/", views.PersonDetailView.as_view(), name="person_detail"),
    path("people/<uuid:pk>/edit/", views.PersonUpdateView.as_view(), name="person_update"),
    path("people/<uuid:pk>/delete/", views.PersonDeleteView.as_view(), name="person_delete"),
]
