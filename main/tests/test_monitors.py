"""Application monitors: real HTTP probes against a local server, the up/down state machine,
alerts, scheduling, stats and the pages."""

import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from apps.alerts.models import Alert, AlertKind
from apps.monitors import checker, stats
from apps.monitors.models import Monitor, MonitorCheck, MonitorStatus, validate_status_spec

pytestmark = pytest.mark.django_db


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(2)
        code = {"/ok": 200, "/fail": 503, "/slow": 200, "/missing": 404}.get(self.path, 200)
        if self.path == "/moved":
            self.send_response(301)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        body = b"status: healthy" if code == 200 else b"oops"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def make(url, **kw):
    return Monitor.objects.create(name=kw.pop("name", url.rsplit("/", 1)[-1] or "m"), url=url, **kw)


# ---------------------------------------------------------------- probing


def test_probe_ok_with_keyword_and_redirect(site):
    r = checker.probe(make(f"{site}/moved", keyword="healthy"))
    assert r.ok and r.status_code == 200 and r.response_ms is not None


def test_probe_failures_are_explained(site):
    assert checker.probe(make(f"{site}/fail")).error == "HTTP 503"
    assert "keyword" in checker.probe(make(f"{site}/ok", name="kw", keyword="nope")).error
    slow = checker.probe(make(f"{site}/slow", timeout_seconds=1))
    assert not slow.ok and slow.error == "timed out"
    refused = checker.probe(make("http://127.0.0.1:9/", name="refused"))
    assert not refused.ok and refused.error == "connection refused"


def test_expected_status_can_accept_404(site):
    assert checker.probe(make(f"{site}/missing", expected_status="200-299,404")).ok


def test_status_spec_validation():
    validate_status_spec("200-399")
    validate_status_spec("200, 204,301-302")
    for bad in ("2xx", "399-200", "700", ""):
        with pytest.raises(ValidationError):
            validate_status_spec(bad)


def test_head_with_keyword_is_rejected():
    m = Monitor(name="h", url="https://x.test", method="HEAD", keyword="ok")
    with pytest.raises(ValidationError):
        m.full_clean()


# ---------------------------------------------------------------- state machine + alerts


def test_one_failure_retries_fast_two_mean_down_and_alert(site):
    m = make(f"{site}/fail", interval_seconds=300)
    now = timezone.now()
    m = checker.record(m, checker.Result(ok=False, error="HTTP 503"), now=now)
    assert m.status == MonitorStatus.PENDING and m.consecutive_failures == 1
    assert m.next_check_at == now + timedelta(seconds=checker.RETRY_SECONDS)
    assert not Alert.objects.exists()  # one blip never pages

    m = checker.record(m, checker.Result(ok=False, error="HTTP 503"), now=now)
    assert m.status == MonitorStatus.DOWN and m.next_check_at == now + timedelta(seconds=300)
    alert = Alert.objects.get(monitor=m, kind=AlertKind.APP_DOWN)
    assert alert.is_open and "HTTP 503" in alert.message and alert.server is None

    checker.record(m, checker.Result(ok=False, error="HTTP 503"))  # still down: no new alert
    assert Alert.objects.filter(monitor=m).count() == 1

    m = checker.record(m, checker.Result(ok=True, status_code=200, response_ms=12))
    assert m.status == MonitorStatus.UP and m.consecutive_failures == 0
    alert.refresh_from_db()
    assert not alert.is_open
    assert MonitorCheck.objects.filter(monitor=m).count() == 4


def test_certificate_alerts(monkeypatch):
    m = make("https://app.test/", name="tls")
    soon = timezone.now() + timedelta(days=5, hours=1)
    monkeypatch.setattr(checker, "cert_expiry", lambda _m: soon)
    checker.update_certificate(m)
    alert = Alert.objects.get(monitor=m, kind=AlertKind.CERT)
    assert alert.severity == "warning" and "5 days" in alert.message
    m.refresh_from_db()
    assert m.tls_days_left == 5
    # Renewed: resolves on the next (12 h later) certificate check.
    monkeypatch.setattr(checker, "cert_expiry", lambda _m: timezone.now() + timedelta(days=80))
    checker.update_certificate(m, now=timezone.now() + timedelta(hours=13))
    alert.refresh_from_db()
    assert not alert.is_open


def test_check_end_to_end(site):
    m = checker.check(make(f"{site}/ok"))
    assert m.status == MonitorStatus.UP and m.last_status_code == 200 and m.status_since


# ---------------------------------------------------------------- scheduling


def test_checker_runs_only_due_enabled_monitors_once(site, monkeypatch):
    due = make(f"{site}/ok", name="due")
    make(f"{site}/ok", name="later", next_check_at=timezone.now() + timedelta(minutes=5))
    make(f"{site}/ok", name="paused", is_enabled=False)
    c = checker.Checker(threading.Event(), workers=1)
    submitted = []
    # Run work inline: the test DB transaction isn't visible to pool threads.
    monkeypatch.setattr(c.pool, "submit", lambda fn, m: submitted.append(m))
    assert c.tick() == 1
    assert c.tick() == 0  # in flight and pushed out: never started twice
    due.refresh_from_db()
    assert due.next_check_at > timezone.now() + timedelta(seconds=50)
    monkeypatch.setattr(checker, "close_old_connections", lambda: None)
    c._run(submitted[0])
    due.refresh_from_db()
    assert due.status == MonitorStatus.UP and due.checks.count() == 1
    assert c.tick() == 0 and not c.in_flight
    c.pool.shutdown(wait=False)


# ---------------------------------------------------------------- stats


def test_uptime_timeline_and_incidents():
    m = make("https://stats.test/", name="stats")
    now = timezone.now()
    for minutes, ok in [(50, True), (40, False), (30, False), (20, True), (10, True)]:
        MonitorCheck.objects.create(
            monitor=m, checked_at=now - timedelta(minutes=minutes), ok=ok, response_ms=100
        )
    assert stats.uptime_by_monitor([m.pk], 1)[m.pk] == pytest.approx(60.0)
    bar = stats.timelines([m.pk], hours=1, buckets=6)[m.pk]
    assert [b["state"] for b in bar].count("down") == 2 and len(bar) == 6
    (incident,) = stats.incidents(m)
    assert incident["duration"] == timedelta(minutes=20)
    series = stats.response_series(m, "1h")
    assert len(series["points"]) == 5 and series["ymax"] == 100


# ---------------------------------------------------------------- pages + permissions


def test_pages_render(client, viewer, site):
    m = checker.check(make(f"{site}/ok"))
    client.force_login(viewer)
    page = client.get(reverse("monitors:list")).content.decode()
    assert m.name in page and "ubar-up" in page
    detail = client.get(reverse("monitors:detail", args=[m.pk]))
    assert detail.status_code == 200 and b'data-unit="ms"' in detail.content
    part = client.get(
        reverse("monitors:detail", args=[m.pk]) + "?part=live", HTTP_HX_REQUEST="true"
    )
    assert b'id="live"' in part.content and b"<html" not in part.content


def test_admin_adds_monitor_operator_cannot(client, admin_user, operator):
    client.force_login(operator)
    assert client.get(reverse("monitors:create")).status_code == 403
    client.force_login(admin_user)
    resp = client.post(
        reverse("monitors:create"),
        {
            "name": "Shop",
            "url": "https://shop.example.com/health",
            "method": "GET",
            "interval_seconds": 60,
            "timeout_seconds": 10,
            "expected_status": "200-399",
            "failure_threshold": 2,
            "verify_tls": "on",
            "is_enabled": "on",
        },
    )
    m = Monitor.objects.get(name="Shop")
    assert resp["Location"] == reverse("monitors:detail", args=[m.pk])
    assert m.next_check_at <= timezone.now() and m.created_by == admin_user


def test_rejects_non_http_urls(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(
        reverse("monitors:create"),
        {"name": "x", "url": "ftp://files.example.com", "method": "GET", "interval_seconds": 60,
         "timeout_seconds": 10, "expected_status": "200", "failure_threshold": 2},
    )  # fmt: skip
    assert resp.status_code == 200 and not Monitor.objects.exists()


def test_pause_resolves_alerts_and_check_now(client, operator, site):
    m = make(f"{site}/fail", failure_threshold=1)
    checker.check(m)
    assert Alert.objects.get(monitor=m).is_open
    client.force_login(operator)
    client.post(reverse("monitors:toggle", args=[m.pk]))
    m.refresh_from_db()
    assert not m.is_enabled and not Alert.objects.get(monitor=m).is_open
    resp = client.post(reverse("monitors:check", args=[m.pk]))
    assert resp.status_code == 302 and m.checks.count() == 2


def test_alerts_page_lists_monitor_alerts(client, viewer, site):
    m = checker.check(make(f"{site}/fail", failure_threshold=1))
    client.force_login(viewer)
    page = client.get(reverse("alerts:list")).content.decode()
    assert reverse("monitors:detail", args=[m.pk]) in page
