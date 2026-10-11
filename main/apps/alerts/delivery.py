"""Alert notifications: Telegram and webhooks, sent from a database outbox.

The relay process calls `process()` every few seconds. For every enabled channel it works
out what that channel still owes: an "opened" message for each new matching alert, a
"resolved" one when it closes, and reminders while it stays open and unacknowledged.
Everything is derived from the alerts and the Delivery rows, so it survives restarts,
retries failures with backoff, and never sends the same opened/resolved message twice.

Maintenance windows hold notifications: an alert opened during one is not announced while
the window lasts. If it's still open afterwards, it's announced then ("held during
maintenance"); if it recovered meanwhile, nobody is bothered.

Everything goes out over HTTPS: no inbound connection, no public API.
"""

import html
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.timesince import timesince

from .maintenance import coverage, was_covered
from .models import Alert, Channel, ChannelKind, Delivery, DeliveryEvent, Severity

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
TIMEOUT = 10
TELEGRAM_API = "https://api.telegram.org"


class SendError(Exception):
    pass


# --------------------------------------------------------------------------- transports


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "ServerMonitor/2"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(65536)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read(4096) or b"{}").get("description", "")
        except ValueError, AttributeError, OSError:
            pass
        raise SendError(f"HTTP {exc.code}{': ' + detail if detail else ''}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise SendError(str(getattr(exc, "reason", exc))[:200]) from exc
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}  # webhooks often answer "ok" in plain text


def telegram_send(token: str, chat_id: str, text_html: str) -> None:
    reply = _post_json(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    if reply and not reply.get("ok", True):
        raise SendError(reply.get("description", "Telegram refused the message"))


def telegram_chats(token: str) -> list[dict]:
    """Chats that recently messaged the bot: how the user finds their chat id without
    any inbound webhook. They send the bot a message first, then we read getUpdates."""
    try:
        with urllib.request.urlopen(
            f"{TELEGRAM_API}/bot{token}/getUpdates?limit=100", timeout=TIMEOUT
        ) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SendError(
            "Telegram rejected this bot token" if exc.code in (401, 404) else f"HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SendError(f"could not reach Telegram: {getattr(exc, 'reason', exc)}") from exc
    chats = {}
    for update in data.get("result", []):
        message = (
            update.get("message")
            or update.get("channel_post")
            or update.get("my_chat_member")
            or {}
        )
        chat = message.get("chat") or {}
        if "id" in chat:
            name = (
                chat.get("title")
                or " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
                or chat.get("username", "")
            )
            chats[chat["id"]] = {"id": str(chat["id"]), "name": name, "type": chat.get("type", "")}
    return list(chats.values())


def webhook_send(url: str, text: str) -> None:
    # `text` for Slack, Mattermost and Teams workflows; `content` for Discord.
    _post_json(url, {"text": text, "content": text})


# --------------------------------------------------------------------------- messages


def _link(alert) -> str:
    base = settings.DASHBOARD_URL.rstrip("/")
    if not base:
        return ""
    if alert.monitor_id:
        return f"{base}/apps/{alert.monitor_id}/"
    return f"{base}/servers/{alert.server_id}/"


def compose(alert: Alert, event: str, note: str = "") -> tuple[str, str]:
    """(Telegram HTML, plain text) for one alert event."""
    where = ""
    if alert.server_id:
        where = f"{alert.server.name} · {alert.server.get_environment_display()}"
    elif alert.monitor_id:
        where = f"{alert.monitor.name} · {alert.monitor.url}"
    if event == DeliveryEvent.RESOLVED:
        head = f"✅ RESOLVED · {alert.get_kind_display()}"
        tail = f"after {timesince(alert.opened_at, alert.resolved_at or timezone.now())}"
    elif event == DeliveryEvent.REPEAT:
        head = f"🔁 STILL OPEN · {alert.get_kind_display()}"
        tail = f"open for {timesince(alert.opened_at)}, not acknowledged"
    else:
        icon = "🔴" if alert.severity == Severity.CRITICAL else "🟠"
        head = f"{icon} {alert.get_severity_display().upper()} · {alert.get_kind_display()}"
        tail = f"since {timezone.localtime(alert.opened_at):%H:%M %Z}"
    link = _link(alert)
    lines_plain = [head, alert.message, where, tail + (f" · {note}" if note else "")]
    lines_html = [
        f"<b>{html.escape(head)}</b>",
        html.escape(alert.message),
        f"<i>{html.escape(where)}</i>" if where else "",
        html.escape(tail + (f" · {note}" if note else "")),
    ]
    if link:
        lines_plain.append(link)
        lines_html.append(
            f'<a href="{html.escape(link)}">Open in {html.escape(settings.SITE_NAME)}</a>'
        )
    return "\n".join(x for x in lines_html if x), "\n".join(x for x in lines_plain if x)


def send(channel: Channel, text_html: str, text_plain: str) -> None:
    if channel.kind == ChannelKind.TELEGRAM:
        if not (channel.telegram_token and channel.telegram_chat_id):
            raise SendError("Telegram bot token and chat id are both required")
        telegram_send(channel.telegram_token, channel.telegram_chat_id, text_html)
    else:
        if not channel.webhook_url:
            raise SendError("webhook URL is missing")
        webhook_send(channel.webhook_url, text_plain)


def send_test(channel: Channel, user=None) -> Delivery:
    who = f" by {user.get_username()}" if user else ""
    text = f"Test notification from {settings.SITE_NAME}{who}. This channel works."
    delivery = Delivery(channel=channel, event=DeliveryEvent.TEST, attempts=1)
    try:
        send(channel, f"<b>✅ {html.escape(text)}</b>", f"✅ {text}")
        delivery.ok, delivery.sent_at = True, timezone.now()
    except SendError as exc:
        delivery.error = str(exc)[:300]
    delivery.save()
    _mark_channel(channel, delivery)
    return delivery


# --------------------------------------------------------------------------- outbox


def _mark_channel(channel: Channel, delivery: Delivery) -> None:
    Channel.objects.filter(pk=channel.pk).update(
        last_delivery_at=timezone.now(), last_delivery_ok=delivery.ok, last_error=delivery.error
    )


def _attempt(
    channel: Channel, alert: Alert, event: str, delivery: Delivery | None, now, note=""
) -> bool:
    """Try one send, recording the outcome. Returns True if it went out."""
    if delivery is None:
        try:
            with transaction.atomic():
                delivery = Delivery.objects.create(channel=channel, alert=alert, event=event)
        except IntegrityError:
            return False  # another relay process got here first
    text_html, text_plain = compose(alert, event, note)
    delivery.attempts += 1
    try:
        send(channel, text_html, text_plain)
        delivery.ok, delivery.sent_at, delivery.error, delivery.next_attempt_at = (
            True,
            now,
            "",
            None,
        )
    except SendError as exc:
        delivery.error = str(exc)[:300]
        # 30 s, 1 m, 2 m, 4 m, then give up (the alert is still on the dashboard).
        delivery.next_attempt_at = (
            now + timedelta(seconds=30 * 2 ** (delivery.attempts - 1))
            if delivery.attempts < MAX_ATTEMPTS
            else None
        )
        logger.warning("Notification to %s failed (%s): %s", channel.name, event, exc)
    delivery.save()
    _mark_channel(channel, delivery)
    return delivery.ok


def _retry_due(delivery: Delivery | None, now) -> bool:
    return (
        delivery is not None
        and not delivery.ok
        and delivery.attempts < MAX_ATTEMPTS
        and delivery.next_attempt_at is not None
        and delivery.next_attempt_at <= now
    )


def process(now=None) -> int:
    """Send everything every enabled channel still owes. Returns messages sent."""
    now = now or timezone.now()
    channels = list(Channel.objects.filter(is_enabled=True))
    if not channels:
        return 0
    cov = coverage(now)
    sent = 0
    open_alerts = list(
        Alert.objects.filter(resolved_at__isnull=True).select_related("server", "monitor__server")
    )
    recently_resolved = list(
        Alert.objects.filter(resolved_at__gte=now - timedelta(days=1)).select_related(
            "server", "monitor__server"
        )
    )
    for channel in channels:
        done = {
            (d.alert_id, d.event): d
            for d in Delivery.objects.filter(
                channel=channel, event__in=[DeliveryEvent.OPENED, DeliveryEvent.RESOLVED]
            ).filter(alert__in=[a.pk for a in open_alerts + recently_resolved])
        }
        for alert in open_alerts:
            # Only alerts raised after the channel existed: adding a channel doesn't replay
            # history.
            if alert.opened_at < channel.created_at or not channel.matches(alert):
                continue
            if cov.covers_alert(alert):
                continue  # held during maintenance
            opened = done.get((alert.pk, DeliveryEvent.OPENED))
            if opened is None:
                late = now - alert.opened_at > timedelta(minutes=2)
                note = "held during maintenance" if late and was_covered(alert) else ""
                sent += _attempt(channel, alert, DeliveryEvent.OPENED, None, now, note)
            elif _retry_due(opened, now):
                sent += _attempt(channel, alert, DeliveryEvent.OPENED, opened, now)
            elif opened.ok and channel.repeat_minutes and alert.acknowledged_at is None:
                last = (
                    Delivery.objects.filter(channel=channel, alert=alert, ok=True)
                    .order_by("-sent_at")
                    .values_list("sent_at", flat=True)
                    .first()
                )
                if last and now - last >= timedelta(minutes=channel.repeat_minutes):
                    sent += _attempt(channel, alert, DeliveryEvent.REPEAT, None, now)
        if channel.notify_resolved:
            for alert in recently_resolved:
                opened = done.get((alert.pk, DeliveryEvent.OPENED))
                if opened is None or not opened.ok:
                    continue  # never announced (or recovered during maintenance): stay quiet
                resolved = done.get((alert.pk, DeliveryEvent.RESOLVED))
                if resolved is None:
                    sent += _attempt(channel, alert, DeliveryEvent.RESOLVED, None, now)
                elif _retry_due(resolved, now):
                    sent += _attempt(channel, alert, DeliveryEvent.RESOLVED, resolved, now)
    return sent


def prune(days: int = 30) -> int:
    return Delivery.objects.filter(created_at__lt=timezone.now() - timedelta(days=days)).delete()[0]
