from django.urls import path

from . import views

app_name = "automations"
urlpatterns = [
    path("", views.AutomationListView.as_view(), name="list"),
    path("new/", views.AutomationCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.AutomationDetailView.as_view(), name="detail"),
    path("<uuid:pk>/edit/", views.AutomationUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.AutomationDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/run/", views.RunNowView.as_view(), name="run_now"),
    path("<uuid:pk>/secret/", views.SecretView.as_view(), name="secret"),
    path("<uuid:pk>/runs/<uuid:run>/", views.RunDetailView.as_view(), name="run"),
    path("<uuid:pk>/runs/<uuid:run>/<str:decision>/", views.DecideView.as_view(), name="decide"),
]
