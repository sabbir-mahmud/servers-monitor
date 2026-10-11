"""Installable app: web manifest, service worker and the offline page.

The service worker caches only static files and the offline page, never dashboard pages or
data: those need you signed in and must not be stale. It also shows Web Push alerts
(see apps/alerts/webpush.py). Served from the site root so its scope covers every page.
"""

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control


@cache_control(max_age=86400)
def manifest(request):
    icons = [
        {"src": static("img/icon.svg"), "sizes": "any", "type": "image/svg+xml"},
        {"src": static("img/icon-192.png"), "sizes": "192x192", "type": "image/png"},
        {"src": static("img/icon-512.png"), "sizes": "512x512", "type": "image/png"},
        {"src": static("img/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ]  # fmt: skip
    return JsonResponse(
        {"name": settings.SITE_NAME, "short_name": settings.SITE_NAME[:12], "id": "/",
         "start_url": "/", "scope": "/", "display": "standalone",
         "background_color": "#f3f3f0", "theme_color": "#3b62d9", "icons": icons},
        content_type="application/manifest+json",
    )  # fmt: skip


@cache_control(no_cache=True)  # browsers re-check it so a new version installs promptly
def service_worker(request):
    response = render(request, "pwa/sw.js", {"version": settings.APP_VERSION},
                      content_type="text/javascript; charset=utf-8")  # fmt: skip
    response["Service-Worker-Allowed"] = "/"
    return response


def offline(request):
    return render(request, "pwa/offline.html")
