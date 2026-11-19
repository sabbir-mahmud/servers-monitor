"""Public status pages served by the dashboard."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind, MaintenanceWindow
from apps.monitors.models import Monitor, MonitorCheck, MonitorDaily, StatusPage

from .conftest import acme, make_account

pytestmark = pytest.mark.django_db


def monitor(name, account=None, **kw):
    return Monitor.objects.create(account=account or acme(), name=name, kind="http",
                                  url="https://internal.example/health", **kw)  # fmt: skip


@pytest.fixture
def page():
    web, api = monitor("Website", status="up"), monitor("API", status="down")
    p = StatusPage.objects.create(account=acme(), title="Acme status", slug="acme")
    p.monitors.set([web, api])
    return p


def test_public_page_needs_no_sign_in_and_hides_internals(client, page):
    web = page.monitors.get(name="Website")
    MonitorDaily.objects.create(monitor=web, day=timezone.localdate() - timedelta(days=3),
                                up=95, total=100)  # fmt: skip
    resp = client.get("/status/acme/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Acme status" in html and "Website" in html and "API" in html
    assert "One system is down" in html and "95.0% up" in html
    assert "internal.example" not in html and "side-nav" not in html
    assert resp["X-Robots-Tag"] == "noindex"


def test_all_up_and_incidents(client, page):
    page.monitors.filter(name="API").update(status="up")
    api = page.monitors.get(name="API")
    Alert.objects.create(account=acme(), monitor=api, kind=AlertKind.APP_DOWN, severity="critical",
                         message="secret error text from 10.0.0.5",
                         resolved_at=timezone.now())  # fmt: skip
    html = client.get("/status/acme/").content.decode()
    assert "All systems operational" in html and "API was down" in html
    assert "10.0.0.5" not in html


def test_maintenance_is_shown(client, page):
    now = timezone.now()
    w = MaintenanceWindow.objects.create(account=acme(), title="Database upgrade",
                                         starts_at=now + timedelta(hours=2),
                                         ends_at=now + timedelta(hours=3))  # fmt: skip
    w.monitors.add(page.monitors.get(name="Website"))
    assert "Database upgrade" in client.get("/status/acme/").content.decode()


def test_draft_is_only_for_the_account(client, page, viewer, django_user_model):
    page.is_published = False
    page.save()
    assert client.get("/status/acme/").status_code == 404
    client.force_login(viewer)
    assert client.get("/status/acme/").status_code == 200


def test_today_counts_from_raw_checks(client, page):
    web = page.monitors.get(name="Website")
    MonitorCheck.objects.create(monitor=web, ok=False)
    assert "0.0% up" in client.get("/status/acme/").content.decode()


def test_admin_creates_a_page_only_with_own_monitors(client, admin_user):
    client.force_login(admin_user)
    mine = monitor("Website")
    theirs = monitor("Other", account=make_account("Other Co"))
    resp = client.post(reverse("monitors:status_page_create"), {
        "title": "Status", "slug": "status", "monitors": [theirs.pk], "is_published": "on",
    })  # fmt: skip
    assert resp.status_code == 200 and not StatusPage.objects.exists()
    resp = client.post(reverse("monitors:status_page_create"), {
        "title": "Status", "slug": "status", "monitors": [mine.pk], "is_published": "on",
        "show_incidents": "on",
    })  # fmt: skip
    assert resp.status_code == 302
    page = StatusPage.objects.get()
    assert "/status/status/" in client.get(reverse("monitors:status_pages")).content.decode()
    client.post(reverse("monitors:status_page_delete", args=[page.pk]))
    assert not StatusPage.objects.exists()


def test_viewer_cannot_edit(client, viewer, page):
    client.force_login(viewer)
    assert client.get(reverse("monitors:status_page_update", args=[page.pk])).status_code == 403


def test_unknown_slug(client):
    assert client.get("/status/nope/").status_code == 404
