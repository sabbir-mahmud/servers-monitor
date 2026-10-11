"""Phase 2.0–2.2: monitor kinds, new check types, heartbeat (cron) monitors."""

import json
import socket
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.agent_api import signing
from apps.agent_api.bridge import handle
from apps.alerts.models import Alert, AlertKind, Severity
from apps.monitors import checker
from apps.monitors.models import Kind, Monitor, MonitorCheck, MonitorStatus

from .conftest import make_server

pytestmark = pytest.mark.django_db


@pytest.fixture
def private_ok(settings):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = True


def _monitor(kind=Kind.HTTP, **kw):
    kw.setdefault("name", f"{kind}-mon")
    kw.setdefault("failure_threshold", 1)
    return Monitor.objects.create(kind=kind, **kw)


# --------------------------------------------------------------------------- HTTP additions


class _App(BaseHTTPRequestHandler):
    seen = {}

    def _reply(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/moved":
            return self._reply(301, headers=[("Location", "/page")])
        if self.path == "/slow":
            import time

            time.sleep(0.25)
        self._reply(
            200, b"<h1>Under maintenance</h1>" if self.path == "/maint" else b"<h1>Shop</h1>"
        )

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        _App.seen = {"body": body, "auth": self.headers.get("Authorization")}
        self._reply(201)

    def log_message(self, *a):
        pass


@pytest.fixture
def app_url(private_ok):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _App)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_existing_monitors_default_to_http(app_url):
    m = Monitor.objects.create(name="old", url=f"{app_url}/page")
    assert m.kind == Kind.HTTP and checker.probe(m).ok


def test_keyword_absent(app_url):
    m = _monitor(url=f"{app_url}/page", keyword="maintenance", options={"keyword_absent": True})
    assert checker.probe(m).ok
    m.url = f"{app_url}/maint"
    r = checker.probe(m)
    assert not r.ok and "appeared" in r.error


def test_redirects_can_be_judged_instead_of_followed(app_url):
    m = _monitor(url=f"{app_url}/moved", expected_status="200-299")
    assert checker.probe(m).ok  # followed to /page
    m.options = {"follow_redirects": False}
    r = checker.probe(m)
    assert not r.ok and r.status_code == 301
    m.expected_status = "301"
    assert checker.probe(m).ok


def test_post_with_body_and_secret_headers(app_url):
    m = _monitor(url=f"{app_url}/api", method="POST", expected_status="201",
                 options={"body": '{"ping":1}'},
                 secret_options=json.dumps({"headers": {"Authorization": "Bearer s3cret"}}))  # fmt: skip
    assert checker.probe(m).ok
    assert _App.seen == {"body": b'{"ping":1}', "auth": "Bearer s3cret"}
    from django.db import connection

    with connection.cursor() as cur:
        cur.execute("SELECT secret_options FROM monitors_monitor WHERE id = %s", [m.pk.hex])
        assert "s3cret" not in cur.fetchone()[0]  # encrypted at rest


def test_slow_is_degraded_not_down(app_url):
    m = _monitor(url=f"{app_url}/slow", options={"slow_ms": 50})
    m = checker.check(m)
    assert m.status == MonitorStatus.DEGRADED and m.last_error.startswith("slow:")
    assert MonitorCheck.objects.get(monitor=m).degraded
    assert not Alert.objects.filter(monitor=m).exists()  # slow never pages


# --------------------------------------------------------------------------- TCP


def test_tcp_open_and_closed_ports(private_ok):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        assert checker.probe(_monitor(Kind.TCP, target="127.0.0.1", options={"port": port})).ok
    finally:
        listener.close()
    r = checker.probe(_monitor(Kind.TCP, name="closed", target="127.0.0.1", options={"port": port}))
    assert not r.ok and r.error == "connection refused"


def test_tcp_respects_netguard(settings, monkeypatch):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    monkeypatch.setattr(
        checker.netguard, "create_connection", lambda *a, **k: pytest.fail("connected")
    )
    for target in ("127.0.0.1", "10.1.2.3", "localhost"):
        # Refused before any connection: closed vs open internal ports can't be told apart.
        r = checker.probe(_monitor(Kind.TCP, name=target, target=target, options={"port": 22}))
        assert not r.ok and "internal address" in r.error


# --------------------------------------------------------------------------- ping

LINUX_OK = """PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
--- 1.1.1.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 11.204/12.871/14.002/1.130 ms
"""
LOSSY = LINUX_OK.replace("3 received, 0%", "2 received, 33.3333%")
DEAD = "3 packets transmitted, 0 received, 100% packet loss, time 2040ms\n"


@pytest.mark.parametrize(
    "out,ok,degraded,ms",
    [(LINUX_OK, True, False, 12), (LOSSY, True, True, 12), (DEAD, False, False, None)],
)
def test_ping_parsing(monkeypatch, out, ok, degraded, ms):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(stdout=out, returncode=0 if ok else 1)

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    r = checker.probe(_monitor(Kind.PING, target="1.1.1.1"))
    assert (r.ok, r.degraded, r.response_ms) == (ok, degraded, ms)
    assert calls[0][-1] == "1.1.1.1"  # pings the checked IP, never re-resolves


def test_ping_refuses_internal_targets(settings, monkeypatch):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    monkeypatch.setattr(checker.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    for target in ("10.0.0.5", "169.254.169.254", "localhost"):
        r = checker.probe(_monitor(Kind.PING, name=target, target=target))
        assert not r.ok and "internal address" in r.error


# --------------------------------------------------------------------------- DNS


class _Answer(list):
    pass


def _fake_resolver(monkeypatch, answers=None, exc=None):
    import dns.resolver

    seen = {}

    def resolve(self, name, rtype):
        seen.update(name=name, rtype=rtype, ns=list(self.nameservers))
        if exc:
            raise exc
        return _Answer(SimpleNamespace(to_text=lambda v=v: v) for v in answers)

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", resolve)
    return seen


def test_dns_expected_answer(monkeypatch):
    seen = _fake_resolver(monkeypatch, ["10 mx1.example.com.", "20 mx2.example.com."])
    m = _monitor(
        Kind.DNS,
        target="example.com",
        options={"record_type": "MX", "expected": "mx1.example.com", "resolver": "1.1.1.1"},
    )
    assert checker.probe(m).ok and seen == {"name": "example.com", "rtype": "MX", "ns": ["1.1.1.1"]}
    m.options["expected"] = "mail.other.net"
    r = checker.probe(m)
    assert not r.ok and "expected 'mail.other.net'" in r.error


def test_dns_nxdomain(monkeypatch):
    import dns.resolver

    _fake_resolver(monkeypatch, exc=dns.resolver.NXDOMAIN())
    r = checker.probe(_monitor(Kind.DNS, target="nope.example"))
    assert not r.ok and "NXDOMAIN" in r.error


# --------------------------------------------------------------------------- domain expiry


def _rdap(monkeypatch, days):
    when = (timezone.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"events": [{"eventAction": "registration", "eventDate": "2001-01-01T00:00:00Z"},
                          {"eventAction": "expiration", "eventDate": when}]}  # fmt: skip

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return json.dumps(payload).encode()

    urls = []
    monkeypatch.setattr(
        checker.netguard, "urlopen", lambda req, **k: urls.append(req.full_url) or Resp()
    )
    return urls


@pytest.mark.parametrize("days,ok,severity", [(200, True, None), (20, True, Severity.WARNING),
                                              (3, True, Severity.CRITICAL), (-2, False, None)])  # fmt: skip
def test_domain_expiry(monkeypatch, days, ok, severity):
    urls = _rdap(monkeypatch, days)
    m = checker.check(_monitor(Kind.DOMAIN, target="example.com"))
    assert urls == ["https://rdap.org/domain/example.com"]
    assert (m.status == MonitorStatus.UP) is ok
    alert = Alert.objects.filter(monitor=m, kind=AlertKind.DOMAIN, resolved_at=None).first()
    assert (alert.severity if alert else None) == severity
    m.refresh_from_db()
    assert m.domain_expires_at is not None


# --------------------------------------------------------------------------- heartbeats


@pytest.fixture
def hb(server):
    return Monitor.objects.create(name="Backup", kind=Kind.HEARTBEAT, target="nightly-backup",
                                  server=server, failure_threshold=1,
                                  options={"period_seconds": 86400, "grace_seconds": 3600})  # fmt: skip


def test_heartbeat_first_deadline_is_a_period_away(hb):
    left = hb.next_check_at - timezone.now()
    assert timedelta(hours=24) < left <= timedelta(hours=25)


def test_ping_ok_then_missed_then_failed(hb):
    now = timezone.now()
    m = checker.receive_ping(hb, "ok", now=now)
    assert m.status == MonitorStatus.UP and m.next_check_at == now + timedelta(hours=25)
    m = checker.missed_heartbeat(m, now=now + timedelta(hours=26))
    assert m.status == MonitorStatus.DOWN and "no ping since" in m.last_error
    assert Alert.objects.filter(monitor=m, kind=AlertKind.APP_DOWN, resolved_at=None).exists()
    m = checker.receive_ping(m, "ok", now=now + timedelta(hours=27))
    assert m.status == MonitorStatus.UP
    assert not Alert.objects.filter(monitor=m, resolved_at=None).exists()


def test_failed_job_is_down_at_once_with_exit_code(hb):
    hb.failure_threshold = 3
    hb.save()
    m = checker.receive_ping(hb, "fail", exit_code=2)
    assert m.status == MonitorStatus.DOWN and m.last_error == "job reported failure (exit code 2)"


def test_start_then_ok_measures_runtime(hb):
    start = timezone.now()
    checker.receive_ping(hb, "start", now=start)
    hb.refresh_from_db()
    m = checker.receive_ping(hb, "ok", now=start + timedelta(seconds=95))
    assert m.last_response_ms == 95_000 and m.run_started_at is None


def test_check_now_never_marks_a_heartbeat_down(client, admin_user, hb):
    client.force_login(admin_user)
    client.post(reverse("monitors:check", args=[hb.pk]))
    hb.refresh_from_db()
    assert hb.status == MonitorStatus.PENDING and not MonitorCheck.objects.exists()


def test_checker_ignores_heartbeats_until_deadline(hb):
    c = checker.Checker(threading.Event(), workers=1)
    submitted = []
    c.pool.submit = lambda fn, monitor: submitted.append(monitor)  # run inline below
    assert c.tick() == 0  # not due: no "missed" before the deadline
    Monitor.objects.filter(pk=hb.pk).update(next_check_at=timezone.now() - timedelta(seconds=1))
    assert c.tick() == 1
    hb.refresh_from_db()
    assert hb.next_check_at > timezone.now() + timedelta(hours=24)  # pushed out a full period
    m = checker.check(submitted[0])
    assert m.status == MonitorStatus.DOWN


# ------------------------------------------------------------- pings through the relay


def _keyed(name):
    server, _ = make_server(name, signing_salt=signing.new_salt())
    return server


def _ping(server, key=None, **body):
    message = {"t": "ping", "name": "nightly-backup", "status": "ok", **body}
    key = key or signing.server_key(server)
    handle(server, signing.sign(message, key) if key else message)


def test_signed_ping_from_the_right_server_counts():
    server = _keyed("db-01")
    m = Monitor.objects.create(
        name="B", kind=Kind.HEARTBEAT, target="nightly-backup", server=server
    )
    _ping(server)
    m.refresh_from_db()
    assert m.status == MonitorStatus.UP
    _ping(server, status="fail", exit=1)
    m.refresh_from_db()
    assert m.status == MonitorStatus.DOWN and "exit code 1" in m.last_error


def test_forged_unsigned_and_cross_server_pings_are_ignored():
    owner, other = _keyed("db-01"), _keyed("web-01")
    legacy, _ = make_server("old-01")  # no signing salt
    m = Monitor.objects.create(name="B", kind=Kind.HEARTBEAT, target="nightly-backup", server=owner)
    Monitor.objects.create(name="L", kind=Kind.HEARTBEAT, target="nightly-backup", server=legacy)
    _ping(owner, key=b"x" * 32)  # forged: wrong key
    _ping(other)  # valid, but for another server's monitor
    _ping(legacy)  # unsigned legacy agent
    assert set(Monitor.objects.values_list("status", flat=True)) == {MonitorStatus.PENDING}
    assert m.checks.count() == 0


# --------------------------------------------------------------------------- the form


def _post(client, **data):
    base = {"name": "M", "interval_seconds": 60, "timeout_seconds": 10, "failure_threshold": 2,
            "expected_status": "200-399", "method": "GET", "is_enabled": "on", "follow_redirects": "on"}  # fmt: skip
    return client.post(reverse("monitors:create"), base | data)


def test_form_creates_each_kind(client, admin_user, server, private_ok):
    client.force_login(admin_user)
    cases = [
        {"kind": "tcp", "name": "pg", "target": "db.example.com", "port": 5432},
        {"kind": "ping", "name": "router", "target": "203.0.113.10"},
        {"kind": "dns", "name": "mx", "target": "example.com", "record_type": "MX", "expected": "mx1"},
        {"kind": "domain", "name": "dom", "target": "Example.COM."},
        {"kind": "heartbeat", "name": "cron", "target": "nightly-backup", "server": server.pk,
         "period_seconds": 3600, "grace_seconds": 600},
    ]  # fmt: skip
    for case in cases:
        resp = _post(client, **case)
        assert resp.status_code == 302, (case["kind"], resp.context["form"].errors)
    got = {m.kind: m for m in Monitor.objects.all()}
    assert got["tcp"].options == {"port": 5432} and got["tcp"].where == "db.example.com:5432"
    assert got["dns"].options == {"record_type": "MX", "expected": "mx1"}
    assert got["domain"].target == "example.com" and got["domain"].interval_seconds == 3600
    assert got["heartbeat"].failure_threshold == 1
    assert got["heartbeat"].options == {"period_seconds": 3600, "grace_seconds": 600}


@pytest.mark.parametrize(
    "data,field",
    [({"kind": "tcp", "target": "db.example.com"}, "port"),
     ({"kind": "heartbeat", "target": "backup"}, "server"),
     ({"kind": "heartbeat", "target": "Bad Name!"}, "target"),
     ({"kind": "ping", "target": "not a host"}, "target"),
     ({"kind": "dns", "target": "example.com", "resolver": "10.0.0.2"}, "resolver"),
     ({"kind": "http", "url": ""}, "url"),
     ({"kind": "http", "url": "https://x.example", "headers": "no colon here"}, "headers")],
)  # fmt: skip
def test_form_validation(client, admin_user, settings, data, field):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    client.force_login(admin_user)
    resp = _post(client, **data)
    assert resp.status_code == 200 and field in resp.context["form"].errors


def test_saved_headers_are_never_shown_again(client, admin_user):
    client.force_login(admin_user)
    _post(
        client,
        kind="http",
        url="https://api.example.com/health",
        headers="Authorization: Bearer topsecret",
    )
    m = Monitor.objects.get()
    assert m.headers == {"Authorization": "Bearer topsecret"}
    page = client.get(reverse("monitors:update", args=[m.pk])).content.decode()
    assert "topsecret" not in page and "1 header(s) saved" in page
    data = {"name": "M", "kind": "http", "url": m.url, "interval_seconds": 60, "timeout_seconds": 10,
            "failure_threshold": 2, "expected_status": "200-399", "method": "GET", "is_enabled": "on"}  # fmt: skip
    client.post(reverse("monitors:update", args=[m.pk]), data)  # headers left blank: kept
    m.refresh_from_db()
    assert m.headers == {"Authorization": "Bearer topsecret"}
    client.post(reverse("monitors:update", args=[m.pk]), data | {"clear_headers": "on"})
    m.refresh_from_db()
    assert m.headers == {}
