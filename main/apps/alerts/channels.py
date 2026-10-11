"""Notification channel kinds beyond Telegram and plain webhooks.

Each kind declares its settings (which are secret, which are required) and how it sends a
`Message`. Settings live in Channel.config, one encrypted JSON blob, so adding a kind
needs no migration. Everything goes out over HTTPS (or SMTP for email); user-typed
server URLs pass through netguard like any other outbound request.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from django.conf import settings

from apps.core import netguard

TIMEOUT = 10


class SendError(Exception):
    pass


@dataclass
class Message:
    """One notification, structured so each kind can format it its own way. Unpacks as
    (html, plain) for the original Telegram/webhook senders."""

    title: str
    body: str
    plain: str
    html: str
    url: str = ""
    severity: str = "info"  # critical | warning | info
    event: str = "opened"  # opened | resolved | repeat | test | system
    key: str = ""  # stable per alert: PagerDuty's dedup key

    def __iter__(self):
        return iter((self.html, self.plain))


@dataclass
class Setting:
    name: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""
    placeholder: str = ""
    url: bool = False  # validated as https:// and checked with netguard
    hidden: bool = False  # filled in by the page's script, not typed (browser push)


@dataclass
class Kind:
    key: str
    label: str
    settings: list[Setting]
    send: object  # callable(config: dict, message: Message) -> None
    blurb: str = ""
    display: object = None  # callable(config) -> str, the non-secret "where it goes"
    available: object = field(default=lambda: True)


def _request(url: str, data: bytes, headers: dict, *, guarded: bool = True, method="POST") -> bytes:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "ServerMonitor/2", **headers})  # fmt: skip
    try:
        opener = netguard.urlopen if guarded else urllib.request.urlopen
        with opener(req, timeout=TIMEOUT) as resp:
            return resp.read(65536)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode(errors="replace").strip()
        except OSError:
            pass
        raise SendError(f"HTTP {exc.code}{': ' + detail[:160] if detail else ''}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SendError(str(getattr(exc, "reason", exc))[:200]) from exc


def _json(url: str, payload: dict, headers: dict | None = None, guarded: bool = True) -> bytes:
    return _request(url, json.dumps(payload).encode(),
                    {"Content-Type": "application/json", **(headers or {})}, guarded=guarded)  # fmt: skip


def _host(config: dict, key: str) -> str:
    return urllib.parse.urlsplit(config.get(key, "")).hostname or "—"


# --------------------------------------------------------------------------- email


def send_email(config: dict, msg: Message) -> None:
    from django.core.mail import send_mail

    if not settings.EMAIL_ENABLED:
        raise SendError("email isn't configured on this dashboard (EMAIL_HOST)")
    to = [a.strip() for a in config.get("recipients", "").split(",") if a.strip()]
    body = msg.plain + (f"\n\n{msg.url}" if msg.url and msg.url not in msg.plain else "")
    try:
        send_mail(f"[{settings.SITE_NAME}] {msg.title}", body, None, to)
    except Exception as exc:  # SMTP errors come in many classes
        raise SendError(f"email failed: {exc}"[:200]) from exc


# --------------------------------------------------------------------------- ntfy / Gotify


NTFY_PRIORITY = {"critical": "5", "warning": "4"}


def send_ntfy(config: dict, msg: Message) -> None:
    server = (config.get("server") or "https://ntfy.sh").rstrip("/")
    headers = {
        "Title": msg.title.encode("ascii", "ignore").decode(),  # header values must be ASCII
        "Priority": NTFY_PRIORITY.get(msg.severity, "3") if msg.event == "opened" else "3",
        "Tags": {"critical": "rotating_light", "warning": "warning"}.get(
            msg.severity, "white_check_mark"
        )
        if msg.event != "resolved"
        else "white_check_mark",
    }
    if msg.url:
        headers["Click"] = msg.url
    if config.get("token"):
        headers["Authorization"] = f"Bearer {config['token']}"
    _request(f"{server}/{urllib.parse.quote(config['topic'], safe='')}", msg.body.encode(), headers)


def send_gotify(config: dict, msg: Message) -> None:
    priority = {"critical": 8, "warning": 5}.get(msg.severity, 2) if msg.event == "opened" else 2
    payload = {"title": msg.title, "message": msg.body, "priority": priority}
    if msg.url:
        payload["extras"] = {"client::notification": {"click": {"url": msg.url}}}
    _json(f"{config['server'].rstrip('/')}/message", payload, {"X-Gotify-Key": config["token"]})


# --------------------------------------------------------------------------- Pushover

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def send_pushover(config: dict, msg: Message) -> None:
    payload = {"token": config["app_token"], "user": config["user_key"], "title": msg.title,
               "message": msg.body, "priority": 0}  # fmt: skip
    if msg.event == "opened" and msg.severity == "critical":
        # Emergency: repeats every minute for an hour until acknowledged in the app.
        payload.update(priority=2, retry=60, expire=3600)
    elif msg.event == "opened" and msg.severity == "warning":
        payload["priority"] = 1
    if msg.url:
        payload.update(url=msg.url, url_title=f"Open in {settings.SITE_NAME}")
    _json(PUSHOVER_API, payload, guarded=False)


# --------------------------------------------------------------------------- PagerDuty

PAGERDUTY_API = "https://events.pagerduty.com/v2/enqueue"


def send_pagerduty(config: dict, msg: Message) -> None:
    """Events API v2: alerts become incidents that resolve themselves, keyed by alert."""
    if msg.event == "repeat":
        return  # PagerDuty escalates on its own
    base = {"routing_key": config["routing_key"]}
    if msg.event == "resolved":
        _json(
            PAGERDUTY_API, base | {"event_action": "resolve", "dedup_key": msg.key}, guarded=False
        )
        return
    dedup = msg.key or f"server-monitor-{msg.event}"
    event = base | {
        "event_action": "trigger",
        "dedup_key": dedup,
        "payload": {
            "summary": f"{msg.title}: {msg.body.splitlines()[0] if msg.body else ''}"[:1024],
            "source": settings.SITE_NAME,
            "severity": {"critical": "critical", "warning": "warning"}.get(msg.severity, "info"),
            "custom_details": {"details": msg.body},
        },
    }
    if msg.url:
        event["links"] = [{"href": msg.url, "text": f"Open in {settings.SITE_NAME}"}]
    _json(PAGERDUTY_API, event, guarded=False)
    if msg.event == "test":  # don't leave a test incident paging someone
        _json(PAGERDUTY_API, base | {"event_action": "resolve", "dedup_key": dedup}, guarded=False)


# --------------------------------------------------------------------------- Slack / Discord

COLORS = {"critical": "#d63939", "warning": "#f59f00", "info": "#2fb344"}


def send_slack(config: dict, msg: Message) -> None:
    color = COLORS["info"] if msg.event == "resolved" else COLORS.get(msg.severity, COLORS["info"])
    text = f"*{msg.title}*\n{msg.body}" + (
        f"\n<{msg.url}|Open in {settings.SITE_NAME}>" if msg.url else ""
    )
    _json(config["webhook_url"], {
        "text": f"{msg.title}: {msg.body.splitlines()[0] if msg.body else ''}",  # notification preview
        "attachments": [{"color": color, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]}],
    })  # fmt: skip


def send_discord(config: dict, msg: Message) -> None:
    color = COLORS["info"] if msg.event == "resolved" else COLORS.get(msg.severity, COLORS["info"])
    embed = {"title": msg.title[:256], "description": msg.body[:4000], "color": int(color[1:], 16)}
    if msg.url:
        embed["url"] = msg.url
    _json(config["webhook_url"], {"username": settings.SITE_NAME[:80], "embeds": [embed]})


# --------------------------------------------------------------------------- browser push


def _push_subject() -> str:
    """VAPID contact (RFC 8292): who push services can reach about this sender."""
    if settings.WEBPUSH_CONTACT:
        return settings.WEBPUSH_CONTACT
    from email.utils import parseaddr

    address = parseaddr(settings.DEFAULT_FROM_EMAIL)[1]
    if "@" in address and not address.endswith("@localhost"):
        return f"mailto:{address}"
    return "https://github.com/sabbir-mahmud/servers-monitor"


def send_webpush(config: dict, msg: Message) -> None:
    from . import webpush

    payload = {"title": msg.title[:120], "body": msg.body[:600], "url": msg.url,
               "tag": msg.key or msg.event, "severity": msg.severity}  # fmt: skip
    try:
        body = webpush.encrypt(json.dumps(payload).encode()[: webpush.MAX_PAYLOAD],
                               webpush.unb64u(config["p256dh"]), webpush.unb64u(config["auth"]))  # fmt: skip
    except (ValueError, KeyError) as exc:
        raise SendError("this browser's subscription is damaged: subscribe it again") from exc
    critical = msg.severity == "critical" and msg.event == "opened"
    headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
               "TTL": "86400", "Urgency": "high" if critical else "normal",
               "Authorization": webpush.vapid_header(config["endpoint"], _push_subject())}  # fmt: skip
    try:
        _request(config["endpoint"], body, headers)
    except SendError as exc:
        if str(exc).startswith(("HTTP 404", "HTTP 410")):
            raise SendError("the browser unsubscribed (or was reset): subscribe it again") from exc
        raise


KINDS = {
    k.key: k
    for k in [
        Kind("email", "Email", [Setting("recipients", "Recipients", placeholder="ops@example.com, oncall@example.com",
                                        help="Comma-separated addresses.")],
             send_email, "Uses this dashboard's SMTP settings.",
             display=lambda c: c.get("recipients", "—"), available=lambda: settings.EMAIL_ENABLED),
        Kind("ntfy", "ntfy", [
            Setting("server", "Server", required=False, url=True, placeholder="https://ntfy.sh",
                    help="Leave empty for ntfy.sh, or your own ntfy server."),
            Setting("topic", "Topic", help="Hard-to-guess on ntfy.sh: anyone who knows it can read it."),
            Setting("token", "Access token", secret=True, required=False, help="For protected topics.")],
             send_ntfy, "Free push notifications to the ntfy phone app.",
             display=lambda c: f"{_host({'s': c.get('server') or 'https://ntfy.sh'}, 's')}/{c.get('topic', '')}"),
        Kind("gotify", "Gotify", [
            Setting("server", "Server URL", url=True, placeholder="https://gotify.example.com"),
            Setting("token", "Application token", secret=True)],
             send_gotify, "Self-hosted push notifications.", display=lambda c: _host(c, "server")),
        Kind("pushover", "Pushover", [
            Setting("user_key", "User or group key", secret=True),
            Setting("app_token", "Application token", secret=True, help="Create an application at pushover.net.")],
             send_pushover, "Critical alerts use emergency priority: they repeat until acknowledged.",
             display=lambda c: "Pushover"),
        Kind("pagerduty", "PagerDuty", [
            Setting("routing_key", "Integration (routing) key", secret=True,
                    help="From a service's Events API v2 integration.")],
             send_pagerduty, "Opens incidents and resolves them when alerts recover.",
             display=lambda c: "Events API v2"),
        Kind("slack", "Slack", [Setting("webhook_url", "Webhook URL", secret=True, url=True,
                                        help="Slack app → Incoming Webhooks → add to a channel.")],
             send_slack, "Colour-coded messages.", display=lambda c: "Slack"),
        Kind("discord", "Discord", [Setting("webhook_url", "Webhook URL", secret=True, url=True,
                                            help="Channel settings → Integrations → Webhooks.")],
             send_discord, "Colour-coded embeds.", display=lambda c: "Discord"),
        Kind("webpush", "Browser push", [
            Setting("endpoint", "Subscription", secret=True, url=True, hidden=True),
            Setting("p256dh", "Browser key", secret=True, hidden=True),
            Setting("auth", "Browser secret", secret=True, hidden=True)],
             send_webpush, "Notifications on this computer or phone, even with the dashboard closed. "
             "Add one channel per device. Needs HTTPS.",
             display=lambda c: f"via {_host(c, 'endpoint')}"),
    ]
}  # fmt: skip
