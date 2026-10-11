"""2.4: public status pages (rendering, privacy, publishing, incidents)."""

import base64
import json
import zipfile
from datetime import timedelta
from io import BytesIO

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.monitors.models import Kind, Monitor, MonitorCheck, MonitorStatus
from apps.status import publish
from apps.status.models import StatusIncident, StatusPage, StatusPageMonitor
from apps.status.render import content_hash, render, snapshot

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(server):
    shop = Monitor.objects.create(name="shop-internal", url="https://10.1.2.3:8443/health?token=abc",
                                  status=MonitorStatus.UP, server=server,
                                  last_error="connection refused by 10.1.2.3")  # fmt: skip
    db = Monitor.objects.create(name="pg-primary", kind=Kind.TCP, target="db.internal.corp",
                                options={"port": 5432}, status=MonitorStatus.DOWN)  # fmt: skip
    Monitor.objects.create(name="secret-admin", url="https://admin.example.com")  # not on the page
    p = StatusPage.objects.create(name="public", title="Acme status")
    StatusPageMonitor.objects.create(page=p, monitor=shop, display_name="Shop", position=0)
    StatusPageMonitor.objects.create(page=p, monitor=db, display_name="Database", position=1)
    now = timezone.now()
    MonitorCheck.objects.bulk_create(
        [
            MonitorCheck(monitor=shop, checked_at=now - timedelta(hours=h), ok=h != 3)
            for h in range(1, 48)
        ]
    )
    return p


def test_snapshot_shows_only_chosen_components(page):
    snap = snapshot(page)
    assert [c["name"] for c in snap["components"]] == ["Shop", "Database"]
    assert [c["state"] for c in snap["components"]] == ["operational", "outage"]
    assert snap["status"] == {"level": "partial", "label": "Partial outage"}
    shop = snap["components"][0]
    assert len(shop["days"]) == 90 and 95 < shop["uptime"] < 100


def test_nothing_internal_is_published(page, server):
    _, html, data = render(page)
    for leak in ("10.1.2.3", "8443", "token=abc", "db.internal.corp", "5432", "shop-internal",
                 "pg-primary", "secret-admin", "admin.example.com", "connection refused", server.name):  # fmt: skip
        assert leak not in html and leak not in data, leak
    assert "<script" not in html.lower()  # static, no JavaScript


def test_hash_ignores_render_time_but_sees_changes(page):
    a = content_hash(snapshot(page, timezone.now()))
    assert content_hash(snapshot(page, timezone.now() + timedelta(seconds=30))) == a
    Monitor.objects.filter(name="pg-primary").update(status=MonitorStatus.UP)
    assert content_hash(snapshot(page)) != a


def test_incident_lifecycle_and_overall_state(client, operator, page):
    client.force_login(operator)
    db_entry = page.entries.get(display_name="Database")
    client.post(reverse("status:incident_create", args=[page.pk]),
                {"title": "Database slow", "impact": "major", "status": "investigating",
                 "affected": [db_entry.pk], "message": "Looking into elevated latency."})  # fmt: skip
    incident = StatusIncident.objects.get()
    snap = snapshot(page)
    assert snap["active_incidents"][0]["affects"] == ["Database"]
    assert snap["active_incidents"][0]["updates"][0]["message"] == "Looking into elevated latency."
    page.refresh_from_db()
    assert page.needs_publish
    client.post(reverse("status:incident_update", args=[page.pk, incident.pk]),
                {"status": "resolved", "message": "Fixed."})  # fmt: skip
    incident.refresh_from_db()
    assert incident.resolved_at is not None and incident.updates.count() == 2
    snap = snapshot(page)
    assert not snap["active_incidents"] and snap["past_incidents"][0]["title"] == "Database slow"


def test_viewers_cannot_post_incidents_or_edit(client, viewer, page):
    client.force_login(viewer)
    assert client.get(reverse("status:detail", args=[page.pk])).status_code == 200
    resp = client.post(reverse("status:incident_create", args=[page.pk]),
                       {"title": "x", "impact": "minor", "status": "investigating", "message": "y"})  # fmt: skip
    assert resp.status_code == 403 and not StatusIncident.objects.exists()
    assert client.get(reverse("status:update", args=[page.pk])).status_code == 403


def test_admin_creates_page_with_components_and_hidden_token(client, admin_user, page):
    client.force_login(admin_user)
    shop = Monitor.objects.get(name="shop-internal")
    data = {"name": "customers", "title": "Acme", "publish_kind": "github", "repo": "acme/status",
            "branch": "main", "token": "ghp_SECRET", "show_uptime": "on",
            f"m_{shop.pk.hex}": "on", f"label_{shop.pk.hex}": "Online shop"}  # fmt: skip
    assert client.post(reverse("status:create"), data).status_code == 302
    new = StatusPage.objects.get(name="customers")
    assert (
        new.config["token"] == "ghp_SECRET" and new.public_url == "https://acme.github.io/status/"
    )
    assert list(new.entries.values_list("display_name", flat=True)) == ["Online shop"]
    edit = client.get(reverse("status:update", args=[new.pk])).content.decode()
    assert "ghp_SECRET" not in edit
    client.post(reverse("status:update", args=[new.pk]), data | {"token": ""})  # keeps the token
    new.refresh_from_db()
    assert new.config["token"] == "ghp_SECRET"


def test_preview_and_download(client, viewer, page):
    client.force_login(viewer)
    html = client.get(reverse("status:preview", args=[page.pk])).content.decode()
    assert "Acme status" in html and "Partial outage" in html
    z = zipfile.ZipFile(BytesIO(client.get(reverse("status:download", args=[page.pk])).content))
    assert sorted(z.namelist()) == ["index.html", "status.json"]
    assert json.loads(z.read("status.json"))["status"]["level"] == "partial"


class FakeGitHub:
    def __init__(self):
        self.files, self.calls = {}, []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path.split("?")[0]))
        key = path.split("/contents/")[1].split("?")[0]
        if method == "GET":
            return self.files.get(key)
        assert token == "ghp_x" and body["branch"] == "gh-pages"
        self.files[key] = {"content": body["content"], "sha": f"sha-{len(self.calls)}"}
        return {}


def test_publish_pushes_files_once_per_change(page, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(publish, "_github", gh)
    page.publish_kind = "github"
    page.publish_config = json.dumps({"repo": "acme/status", "branch": "gh-pages", "path": "docs",
                                      "token": "ghp_x", "domain": "status.acme.com"})  # fmt: skip
    page.save()
    assert publish.publish_due() == 1
    assert set(gh.files) == {"docs/index.html", "docs/status.json", "docs/CNAME"}
    assert base64.b64decode(gh.files["docs/CNAME"]["content"]) == b"status.acme.com\n"
    calls = len(gh.calls)
    assert publish.publish_due() == 0 and len(gh.calls) == calls  # nothing changed: no API calls
    Monitor.objects.filter(name="pg-primary").update(status=MonitorStatus.UP)
    assert publish.publish_due() == 1
    page.refresh_from_db()
    assert page.last_error == "" and page.last_published_at is not None


def test_failed_publish_is_recorded_and_backs_off(page, monkeypatch):
    calls = []

    def broken(*a, **k):
        calls.append(a)
        raise publish.PublishError("GitHub HTTP 401: token rejected")

    monkeypatch.setattr(publish, "_github", broken)
    page.publish_kind = "github"
    page.publish_config = json.dumps({"repo": "acme/status", "token": "bad"})
    page.save()
    assert publish.publish_due() == 0
    page.refresh_from_db()
    assert "401" in page.last_error and not page.needs_publish
    assert publish.publish_due() == 0 and len(calls) == 1  # backing off, not hammering GitHub
