"""Notification channels (Telegram, webhooks), the delivery outbox, reminders, retries and
maintenance windows."""

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts import delivery, maintenance
from apps.alerts.models import (
    Alert,
    AlertKind,
    Channel,
    Delivery,
    DeliveryEvent,
    MaintenanceWindow,
    Severity,
)
from apps.fleet.models import Environment
from apps.monitors.models import Monitor

from .conftest import make_server

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- fixtures


class Outbox:
    """Captures what would have been sent; can be told to fail."""

    def __init__(self):
        self.sent, self.fail = [], False

    def __call__(self, channel, message, *_):
        if self.fail:
            raise delivery.SendError("HTTP 502")
        self.sent.append((channel.name, message.plain))


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(delivery, "send", box)
    return box


def channel(**kw):
    defaults = {"name": "ops", "kind": "telegram", "telegram_token": "t", "telegram_chat_id": "1"}
    c = Channel.objects.create(**{**defaults, **kw})
    Channel.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(hours=1))
    c.refresh_from_db()
    return c


def alert(target, kind=AlertKind.OFFLINE, severity=Severity.CRITICAL, **kw):
    field = "server" if hasattr(target, "environment") else "monitor"
    return Alert.objects.create(
        **{field: target}, kind=kind, severity=severity, message=f"{target.name} broke", **kw
    )


@pytest.fixture
def prod():
    return make_server("web-01", environment=Environment.PRODUCTION)[0]


# ---------------------------------------------------------------- outbox


def test_opened_then_resolved_each_sent_exactly_once(outbox, prod):
    channel()
    a = alert(prod)
    assert delivery.process() == 1 and delivery.process() == 0
    a.resolved_at = timezone.now()
    a.save()
    assert delivery.process() == 1 and delivery.process() == 0
    assert [t.split("\n")[0] for _, t in outbox.sent] == [
        "🔴 CRITICAL · Server offline",
        "✅ RESOLVED · Server offline",
    ]


def test_adding_a_channel_does_not_replay_history(outbox, prod):
    a = alert(prod)
    Alert.objects.filter(pk=a.pk).update(opened_at=timezone.now() - timedelta(hours=2))
    channel()
    assert delivery.process() == 0


def test_routing_rules(outbox, prod):
    staging = make_server("stg-01", environment=Environment.STAGING)[0]
    app = Monitor.objects.create(name="shop", url="https://shop.test/")
    channel(name="prod-critical", min_severity=Severity.CRITICAL, environments=["production"])
    channel(name="apps-only", include_servers=False)
    alert(prod)
    alert(staging)
    alert(prod, kind=AlertKind.DISK, severity=Severity.WARNING)
    alert(app, kind=AlertKind.APP_DOWN)
    delivery.process()
    got = {(name, text.split("\n")[1]) for name, text in outbox.sent}
    assert got == {
        ("prod-critical", "web-01 broke"),
        ("prod-critical", "shop broke"),  # apps without a server pass environment filters
        ("apps-only", "shop broke"),
    }


def test_reminders_until_acknowledged(outbox, prod):
    channel(repeat_minutes=15)
    a = alert(prod)
    now = timezone.now()
    delivery.process(now)
    delivery.process(now + timedelta(minutes=10))
    assert len(outbox.sent) == 1
    Delivery.objects.update(sent_at=now)
    delivery.process(now + timedelta(minutes=16))
    assert outbox.sent[-1][1].startswith("🔁 STILL OPEN")
    a.acknowledged_at = timezone.now()
    a.save()
    delivery.process(now + timedelta(hours=2))
    assert len(outbox.sent) == 2


def test_failures_retry_with_backoff_then_give_up(outbox, prod):
    c = channel()
    alert(prod)
    outbox.fail = True
    now = timezone.now()
    delivery.process(now)
    d = Delivery.objects.get()
    assert not d.ok and d.attempts == 1 and d.next_attempt_at == now + timedelta(seconds=30)
    c.refresh_from_db()
    assert c.last_delivery_ok is False and c.last_error == "HTTP 502"
    for minutes in (1, 3, 7, 15, 40):
        delivery.process(now + timedelta(minutes=minutes))
    d.refresh_from_db()
    assert d.attempts == delivery.MAX_ATTEMPTS and d.next_attempt_at is None
    outbox.fail = False
    delivery.process(now + timedelta(hours=1))
    assert outbox.sent == []  # gave up; the alert is still on the dashboard


def test_retry_succeeds(outbox, prod):
    channel()
    alert(prod)
    outbox.fail = True
    now = timezone.now()
    delivery.process(now)
    outbox.fail = False
    delivery.process(now + timedelta(seconds=31))
    assert len(outbox.sent) == 1 and Delivery.objects.get().ok


# ---------------------------------------------------------------- maintenance


def _window(**kw):
    now = timezone.now()
    targets = {k: kw.pop(k) for k in ("servers", "monitors") if k in kw}
    w = MaintenanceWindow.objects.create(
        title="upgrade",
        starts_at=kw.pop("starts_at", now - timedelta(minutes=5)),
        ends_at=kw.pop("ends_at", now + timedelta(hours=1)),
        **kw,
    )
    for name, objs in targets.items():
        getattr(w, name).set(objs)
    return w


def test_maintenance_holds_then_releases_still_open_alerts(outbox, prod):
    channel()
    w = _window(servers=[prod])
    alert(prod)
    assert delivery.process() == 0
    later = timezone.now() + timedelta(hours=2)
    MaintenanceWindow.objects.filter(pk=w.pk).update(ends_at=timezone.now())
    delivery.process(later)
    (text,) = [t for _, t in outbox.sent]
    assert "held during maintenance" in text


def test_recovered_during_maintenance_is_never_sent(outbox, prod):
    channel()
    w = _window(servers=[prod])
    a = alert(prod)
    delivery.process()
    a.resolved_at = timezone.now()
    a.save()
    maintenance.end_now(w)
    delivery.process()
    assert outbox.sent == []


def test_muting_a_server_mutes_its_apps(prod):
    app = Monitor.objects.create(name="api", url="https://api.test/", server=prod)
    other = Monitor.objects.create(name="blog", url="https://blog.test/")
    _window(servers=[prod])
    cov = maintenance.coverage()
    assert cov.covers_monitor(app.pk, app.server_id) and not cov.covers_monitor(other.pk)
    assert cov.covers_alert(alert(app, kind=AlertKind.APP_DOWN))


def test_upcoming_window_does_not_mute_yet(prod):
    now = timezone.now()
    _window(servers=[prod], starts_at=now + timedelta(hours=1), ends_at=now + timedelta(hours=2))
    assert not maintenance.coverage().covers_server(prod.pk)


# ---------------------------------------------------------------- telegram transport


class FakeTelegram(BaseHTTPRequestHandler):
    received = []

    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "/botBAD/" in self.path:
            return self._reply(401, {"ok": False, "description": "Unauthorized"})
        FakeTelegram.received.append((self.path, payload))
        self._reply(200, {"ok": True, "result": {}})

    def do_GET(self):
        if "/botBAD/" in self.path:
            return self._reply(401, {"ok": False})
        self._reply(200, {"ok": True, "result": [
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Sabbir"}}},
            {"message": {"chat": {"id": -100, "type": "group", "title": "Ops team"}}},
            {"message": {"chat": {"id": 42, "type": "private", "first_name": "Sabbir"}}},
        ]})  # fmt: skip

    def log_message(self, *a):
        pass


@pytest.fixture
def telegram(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(delivery, "TELEGRAM_API", f"http://127.0.0.1:{httpd.server_address[1]}")
    FakeTelegram.received = []
    yield FakeTelegram
    httpd.shutdown()


def test_telegram_message_and_chat_discovery(telegram, prod):
    c = channel(telegram_token="TOKEN", telegram_chat_id="42")
    a = alert(prod)
    html_text, _ = delivery.compose(a, DeliveryEvent.OPENED)
    delivery.send(c, html_text, "")
    path, payload = telegram.received[0]
    assert path == "/botTOKEN/sendMessage" and payload["chat_id"] == "42"
    assert (
        payload["parse_mode"] == "HTML" and "<b>🔴 CRITICAL · Server offline</b>" in payload["text"]
    )
    chats = delivery.telegram_chats("TOKEN")
    assert chats == [
        {"id": "42", "name": "Sabbir", "type": "private"},
        {"id": "-100", "name": "Ops team", "type": "group"},
    ]


def test_telegram_errors_are_readable(telegram):
    with pytest.raises(delivery.SendError, match="Unauthorized"):
        delivery.telegram_send("BAD", "1", "hi")
    with pytest.raises(delivery.SendError, match="rejected this bot token"):
        delivery.telegram_chats("BAD")


def test_messages_escape_html(prod):
    a = Alert.objects.create(
        server=prod,
        kind=AlertKind.SERVICE,
        severity=Severity.CRITICAL,
        message="<script>x</script>",
    )
    html_text, _ = delivery.compose(a, DeliveryEvent.OPENED)
    assert "<script>" not in html_text and "&lt;script&gt;" in html_text


# ---------------------------------------------------------------- pages


def test_admin_adds_telegram_channel_secret_never_echoed(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(
        reverse("alerts:channel_create") + "?kind=telegram",
        {"kind": "telegram", "name": "Phone", "telegram_token": "123:SECRET", "telegram_chat_id": "42",
         "min_severity": "warning", "include_servers": "on", "include_apps": "on",
         "notify_resolved": "on", "repeat_minutes": "30", "is_enabled": "on"},
    )  # fmt: skip
    assert resp.status_code == 302
    c = Channel.objects.get(name="Phone")
    assert c.telegram_token == "123:SECRET" and c.repeat_minutes == 30
    edit = client.get(reverse("alerts:channel_update", args=[c.pk])).content.decode()
    assert "123:SECRET" not in edit
    assert "123:SECRET" not in client.get(reverse("alerts:channels")).content.decode()
    # Blank token on edit keeps the saved one.
    client.post(
        reverse("alerts:channel_update", args=[c.pk]),
        {"kind": "telegram", "name": "Phone", "telegram_token": "", "telegram_chat_id": "43",
         "min_severity": "critical", "include_servers": "on", "repeat_minutes": "0", "is_enabled": "on"},
    )  # fmt: skip
    c.refresh_from_db()
    assert c.telegram_token == "123:SECRET" and c.telegram_chat_id == "43"


def test_operators_cannot_manage_channels_but_can_mute(client, operator, prod):
    client.force_login(operator)
    assert client.get(reverse("alerts:channels")).status_code == 403
    resp = client.post(reverse("alerts:quick_mute"), {"server": str(prod.pk), "minutes": "60"})
    assert resp.status_code == 302
    w = MaintenanceWindow.objects.get()
    assert list(w.servers.all()) == [prod] and w.state == "active"
    page = client.get(reverse("fleet:server_detail", args=[prod.pk])).content.decode()
    assert "In maintenance until" in page


def test_maintenance_form_validation(client, operator, prod):
    client.force_login(operator)
    now = timezone.localtime()
    fmt = "%Y-%m-%dT%H:%M"
    resp = client.post(
        reverse("alerts:maintenance_create"),
        {"title": "x", "starts_at": now.strftime(fmt), "ends_at": (now - timedelta(hours=1)).strftime(fmt),
         "servers": [str(prod.pk)]},
    )  # fmt: skip
    assert resp.status_code == 200 and b"must end after" in resp.content
    resp = client.post(
        reverse("alerts:maintenance_create"),
        {"title": "x", "starts_at": now.strftime(fmt), "ends_at": (now + timedelta(hours=1)).strftime(fmt)},
    )  # fmt: skip
    assert b"at least one" in resp.content and not MaintenanceWindow.objects.exists()


def test_alerts_list_marks_muted(client, viewer, prod):
    _window(servers=[prod])
    alert(prod)
    client.force_login(viewer)
    assert "Muted" in client.get(reverse("alerts:list")).content.decode()


def test_new_channel_requires_its_secret(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(
        reverse("alerts:channel_create") + "?kind=telegram",
        {"kind": "telegram", "name": "No token", "telegram_chat_id": "42", "min_severity": "warning",
         "include_servers": "on", "repeat_minutes": "0"},
    )  # fmt: skip
    assert resp.status_code == 200 and not Channel.objects.exists()
    assert b"keep the saved token" not in resp.content
