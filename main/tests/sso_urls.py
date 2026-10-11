"""The dashboard's URLs with single sign-on mounted, as when OIDC_RP_CLIENT_ID is set."""

from django.urls import include, path

from config.urls import urlpatterns as base

urlpatterns = [path("oidc/", include("mozilla_django_oidc.urls")), *base]
