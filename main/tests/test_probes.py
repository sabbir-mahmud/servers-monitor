"""3.4: agents as uptime probes: assignments, results on heartbeats, quorum."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.agent_api import signing
from apps.alerts.models import Alert, AlertKind
from apps.monitors import checker, stats
from apps.monitors.checker import Result
from apps.monitors.forms import MonitorForm
from apps.monitors.models import Kind, Monitor, MonitorCheck, MonitorStatus

from .conftest import AgentClient, make_server, report

pytestmark = pytest.mark.django_db

PROBES = {"features": ["probes"]}


def probe_server(name, **kwargs):
    server = make_server(name, signing_salt="ab" * 16, **kwargs)[0]
    return server


@pytest.fixture
def servers():
    return [probe_server(f"probe-{i}") for i in (1, 2)]


@pytest.fixture
def monitor(servers):
    m = Monitor.objects.create(name="app", url="https://app.example.com/health",
                               failure_threshold=1, quorum=2)  # fmt: skip
    m.probe_servers.set(servers)
    return m


def heartbeat(fake_relay, server, **extra):
    client = AgentClient(server, fake_relay)
    client.heartbeat(report(capabilities=PROBES, **extra))
    return client


def test_assignments_pushed_signed_and_only_when_changed(fake_relay, servers, monitor):
    client = heartbeat(fake_relay, servers[0])
    (cfg,) = [m for m in client.mailbox() if m["t"] == "cfg"]
    assert signing.verify(cfg, signing.server_key(servers[0]))
    (spec,) = cfg["probes"]
    assert spec["id"] == str(monitor.pk) and spec["url"] == monitor.url and "headers" not in spec
    client.heartbeat(report(capabilities=PROBES, probes_rev=cfg["probes_rev"]))
    assert client.mailbox() == []  # in sync: nothing re-sent, no Redis cost
    monitor.probe_servers.remove(servers[0])
    client.heartbeat(report(capabilities=PROBES, probes_rev=cfg["probes_rev"]))
    assert [m["probes"] for m in client.mailbox()] == [[]]


def test_results_stored_per_location_only_for_assigned_monitors(fake_relay, servers, monitor):
    other = Monitor.objects.create(name="other", url="https://other.example.com/")
    results = [{"id": str(monitor.pk), "ok": True, "ms": 42, "code": 200},
               {"id": str(other.pk), "ok": False, "error": "forged"},
               {"id": "not-a-uuid", "ok": False}]  # fmt: skip
    heartbeat(fake_relay, servers[0], probe_results=results)
    rows = MonitorCheck.objects.filter(monitor=monitor).exclude(location="")
    assert [(r.location, r.ok, r.response_ms) for r in rows] == [(str(servers[0].pk), True, 42)]
    assert not MonitorCheck.objects.filter(monitor=other).exists()


def test_opted_out_and_legacy_agents_are_ignored(fake_relay, monitor):
    off = make_server("no-probes", signing_salt="cd" * 16)[0]
    legacy = make_server("legacy")[0]  # unsigned: anyone with Redis access could forge it
    for server in (off, legacy):
        monitor.probe_servers.add(server)
        caps = PROBES if server is legacy else {"features": []}
        client = AgentClient(server, fake_relay)
        client.heartbeat(
            report(capabilities=caps, probe_results=[{"id": str(monitor.pk), "ok": False}])
        )
        assert not [m for m in client.mailbox() if "probes" in m]
    assert not MonitorCheck.objects.filter(monitor=monitor).exists()


def test_quorum_one_of_three_down_is_up_two_is_down(servers, monitor):
    a, b = (str(s.pk) for s in servers)
    checker.record(monitor, Result(ok=True, response_ms=10))  # the dashboard
    checker.record(monitor, Result(ok=False, error="timed out"), location=a)
    m = checker.record(monitor, Result(ok=True, response_ms=12), location=b)
    assert m.status == MonitorStatus.UP and "probe-1 only (1 of 3)" in m.last_error
    m = checker.record(monitor, Result(ok=False, error="HTTP 502"), location=b)
    assert m.status == MonitorStatus.DOWN and m.last_error.startswith("down from probe-1, probe-2")
    alert = Alert.objects.get(monitor=monitor, kind=AlertKind.APP_DOWN, resolved_at=None)
    assert "probe-1" in alert.message
    m = checker.record(monitor, Result(ok=True), location=a)
    assert m.status == MonitorStatus.UP
    assert Alert.objects.get(pk=alert.pk).resolved_at is not None


def test_uptime_uses_the_quorum_outcome(servers, monitor):
    a = str(servers[0].pk)
    checker.record(monitor, Result(ok=True))
    checker.record(monitor, Result(ok=False, error="x"), location=a)  # 1 of 2 live: below quorum
    assert stats.uptime_by_monitor([monitor.pk])[monitor.pk] == 100.0
    assert MonitorCheck.objects.filter(monitor=monitor, ok=False).count() == 1  # the raw row


def test_silent_locations_age_out_of_the_quorum(servers, monitor):
    a, b = (str(s.pk) for s in servers)
    long_ago = timezone.now() - timedelta(hours=2)
    checker.record(monitor, Result(ok=True), now=long_ago, location=b)
    checker.record(monitor, Result(ok=True), now=long_ago)
    m = checker.record(monitor, Result(ok=False, error="refused"), location=a)
    assert m.status == MonitorStatus.DOWN  # the only live location: it decides


def test_agent_only_monitor_is_not_probed_by_the_dashboard(servers, monitor, monkeypatch):
    Monitor.objects.filter(pk=monitor.pk).update(check_from_dashboard=False)
    monitor.refresh_from_db()
    monkeypatch.setattr(checker, "probe", lambda m: pytest.fail("dashboard must not probe"))
    checker.record(monitor, Result(ok=False, error="x"), location=str(servers[0].pk))
    checker.record(monitor, Result(ok=False, error="x"), location=str(servers[1].pk))
    assert checker.check(monitor).status == MonitorStatus.DOWN


def form_data(**extra):
    return {"kind": Kind.HTTP, "name": "internal", "url": "http://10.0.0.5/health",
            "method": "GET", "interval_seconds": 60, "timeout_seconds": 10,
            "expected_status": "200-399", "failure_threshold": 2, **extra}  # fmt: skip


def test_form_allows_internal_urls_only_when_agents_alone_check(servers):
    form = MonitorForm(form_data())
    assert not form.is_valid() and "url" in form.errors  # the dashboard won't fetch internal URLs
    form = MonitorForm(form_data(agents_only="on"))
    assert not form.is_valid() and "probe_servers" in form.errors  # nobody would check it
    form = MonitorForm(form_data(agents_only="on", probe_servers=[s.pk for s in servers], quorum=2))
    assert form.is_valid(), form.errors
    m = form.save()
    assert not m.check_from_dashboard and m.quorum == 2


def test_form_refuses_secret_headers_and_impossible_quorum(servers):
    data = form_data(url="https://app.example.com/", probe_servers=[servers[0].pk])
    form = MonitorForm({**data, "headers": "Authorization: Bearer x"})
    assert not form.is_valid() and "relay would see" in str(form.errors["probe_servers"])
    form = MonitorForm({**data, "quorum": 3})
    assert not form.is_valid() and "quorum" in form.errors


def test_detail_page_shows_each_location(client, admin_user, servers, monitor):
    checker.record(monitor, Result(ok=False, error="HTTP 502", status_code=502),
                   location=str(servers[0].pk))  # fmt: skip
    client.force_login(admin_user)
    page = client.get(reverse("monitors:detail", args=[monitor.pk])).content.decode()
    assert "Locations" in page and "This dashboard" in page and "probe-2" in page
    assert "HTTP 502" in page and "probes are off on this server" in page
