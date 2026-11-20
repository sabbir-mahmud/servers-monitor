from django.urls import path

from . import views

app_name = "deploys"
urlpatterns = [
    path("", views.ProjectListView.as_view(), name="list"),
    path("new/", views.ProjectCreateView.as_view(), name="create"),
    path("<uuid:pk>/", views.ProjectDetailView.as_view(), name="project"),
    path("<uuid:pk>/edit/", views.ProjectUpdateView.as_view(), name="update"),
    path("<uuid:pk>/delete/", views.ProjectDeleteView.as_view(), name="delete"),
    path("<uuid:pk>/deploy/", views.DeployNowView.as_view(), name="deploy"),
    path("<uuid:pk>/secret/", views.SecretView.as_view(), name="secret"),
    path("<uuid:pk>/runs/<uuid:dep>/", views.DeploymentDetailView.as_view(), name="deployment"),
    path(
        "<uuid:pk>/runs/<uuid:dep>/<str:decision>/",
        views.DecideView.as_view(),
        name="decide",
    ),
]
