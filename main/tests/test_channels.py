"""2.3: notification channel kinds (email, ntfy, Gotify, Pushover, PagerDuty, Slack, Discord)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.core import mail
from django.urls import reverse

from apps.alerts import channels, delivery
from apps.alerts.models import Alert, AlertKind, Channel, DeliveryEvent, Severity

pytestmark = pytest.mark.django_db


class _Sink(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"] or 0))
        _Sink.got.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def sink(settings):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = True
    _Sink.got = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", _Sink.got
    httpd.shutdown()


def _channel(kind, **config):
    return Channel.objects.create(name=kind, kind=kind, config=json.dumps(config))


@pytest.fixture
def alert(server):
    return Alert.objects.create(server=server, kind=AlertKind.DISK, severity=Severity.CRITICAL,
                                message="Disk at 97%")  # fmt: skip


def _send(channel, alert, event=DeliveryEvent.OPENED):
    delivery.send(channel, delivery.compose(alert, event))


def test_ntfy(sink, alert):
    url, got = sink
    _send(_channel("ntfy", server=url, topic="ops alerts", token="tk"), alert)
    req = got[0]
    assert req["path"] == "/ops%20alerts" and req["headers"]["Priority"] == "5"
    assert req["headers"]["Authorization"] == "Bearer tk" and b"Disk at 97%" in req["body"]
    assert (
        req["headers"]["Title"] == "CRITICAL · Disk filling up".encode("ascii", "ignore").decode()
    )


def test_gotify(sink, alert):
    url, got = sink
    _send(_channel("gotify", server=url, token="app-token"), alert)
    body = json.loads(got[0]["body"])
    assert got[0]["path"] == "/message" and got[0]["headers"]["X-Gotify-Key"] == "app-token"
    assert body["priority"] == 8 and "Disk at 97%" in body["message"]


def test_pushover_emergency_for_critical(sink, alert, monkeypatch):
    url, got = sink
    monkeypatch.setattr(channels, "PUSHOVER_API", f"{url}/1/messages.json")
    _send(_channel("pushover", user_key="u", app_token="a"), alert)
    body = json.loads(got[0]["body"])
    assert (body["priority"], body["retry"], body["expire"]) == (2, 60, 3600)
    assert body["token"] == "a" and body["user"] == "u"


def test_pagerduty_trigger_then_resolve_with_the_same_key(sink, alert, monkeypatch):
    url, got = sink
    monkeypatch.setattr(channels, "PAGERDUTY_API", f"{url}/v2/enqueue")
    ch = _channel("pagerduty", routing_key="R" * 32)
    _send(ch, alert)
    _send(ch, alert, DeliveryEvent.REPEAT)  # PagerDuty escalates itself: nothing sent
    _send(ch, alert, DeliveryEvent.RESOLVED)
    trigger, resolve = (json.loads(r["body"]) for r in got)
    assert trigger["event_action"] == "trigger" and trigger["payload"]["severity"] == "critical"
    assert resolve == {
        "routing_key": "R" * 32,
        "event_action": "resolve",
        "dedup_key": trigger["dedup_key"],
    }
    assert trigger["dedup_key"] == f"server-monitor-{alert.pk}"


def test_pagerduty_test_message_resolves_itself(sink, monkeypatch):
    url, got = sink
    monkeypatch.setattr(channels, "PAGERDUTY_API", f"{url}/v2/enqueue")
    result = delivery.send_test(_channel("pagerduty", routing_key="R"))
    assert result.ok
    assert [json.loads(r["body"])["event_action"] for r in got] == ["trigger", "resolve"]


def test_slack_and_discord_formatting(sink, alert):
    url, got = sink
    _send(_channel("slack", webhook_url=f"{url}/slack"), alert)
    _send(_channel("discord", webhook_url=f"{url}/discord"), alert, DeliveryEvent.RESOLVED)
    slack, discord = (json.loads(r["body"]) for r in got)
    assert slack["attachments"][0]["color"] == "#d63939"
    assert "*CRITICAL · Disk filling up*" in slack["attachments"][0]["blocks"][0]["text"]["text"]
    assert discord["embeds"][0]["color"] == 0x2FB344 and discord["embeds"][0]["title"].startswith(
        "RESOLVED"
    )


def test_email(settings, alert):
    settings.EMAIL_ENABLED = True
    _send(_channel("email", recipients="a@example.com, b@example.com"), alert)
    (sent,) = mail.outbox
    assert sent.to == ["a@example.com", "b@example.com"] and "CRITICAL" in sent.subject


def test_email_without_smtp_fails_clearly(settings, alert):
    settings.EMAIL_ENABLED = False
    with pytest.raises(delivery.SendError, match="isn't configured"):
        _send(_channel("email", recipients="a@example.com"), alert)


def test_netguard_blocks_internal_targets(settings, alert):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    with pytest.raises(delivery.SendError, match="internal address"):
        _send(_channel("ntfy", server="http://127.0.0.1:9", topic="t"), alert)


def test_missing_settings_are_reported(alert):
    with pytest.raises(delivery.SendError, match="Topic missing"):
        _send(_channel("ntfy"), alert)


# --------------------------------------------------------------------------- the form


def _form(kind, **fields):
    return {"name": f"my {kind}", "kind": kind, "min_severity": "warning", "include_servers": "on",
            "include_apps": "on", "notify_resolved": "on", "repeat_minutes": 0, "is_enabled": "on",
            **{f"cfg_{k}": v for k, v in fields.items()}}  # fmt: skip


def test_create_and_edit_keeps_secrets_hidden(client, admin_user):
    client.force_login(admin_user)
    resp = client.post(reverse("alerts:channel_create") + "?kind=pushover",
                       _form("pushover", user_key="USERKEY", app_token="APPTOKEN"))  # fmt: skip
    assert resp.status_code == 302
    ch = Channel.objects.get()
    assert ch.settings == {"user_key": "USERKEY", "app_token": "APPTOKEN"}
    page = client.get(reverse("alerts:channel_update", args=[ch.pk])).content.decode()
    assert "USERKEY" not in page and "APPTOKEN" not in page and "keep the saved value" in page
    client.post(
        reverse("alerts:channel_update", args=[ch.pk]), _form("pushover", app_token="NEWTOKEN")
    )
    ch.refresh_from_db()
    assert ch.settings == {"user_key": "USERKEY", "app_token": "NEWTOKEN"}


@pytest.mark.parametrize(
    "kind,fields,error_field",
    [("email", {"recipients": "ops@example.com, not-an-address"}, "cfg_recipients"),
     ("gotify", {"server": "http://10.0.0.5", "token": "t"}, "cfg_server"),
     ("slack", {"webhook_url": "nope"}, "cfg_webhook_url"),
     ("pagerduty", {}, "cfg_routing_key")],
)  # fmt: skip
def test_form_validation(client, admin_user, settings, kind, fields, error_field):
    settings.MONITOR_ALLOW_PRIVATE_TARGETS = False
    settings.EMAIL_ENABLED = True
    client.force_login(admin_user)
    resp = client.post(reverse("alerts:channel_create") + f"?kind={kind}", _form(kind, **fields))
    assert resp.status_code == 200 and error_field in resp.context["form"].errors


def test_email_kind_hidden_without_smtp(client, admin_user, settings):
    settings.EMAIL_ENABLED = False
    client.force_login(admin_user)
    page = client.get(reverse("alerts:channel_create")).content.decode()
    assert "?kind=ntfy" in page and "?kind=email" not in page


def test_secrets_encrypted_at_rest():
    from django.db import connection

    ch = _channel("pagerduty", routing_key="TOPSECRETKEY")
    with connection.cursor() as cur:
        cur.execute("SELECT config FROM alerts_channel WHERE id = %s", [ch.pk.hex])
        assert "TOPSECRETKEY" not in cur.fetchone()[0]
