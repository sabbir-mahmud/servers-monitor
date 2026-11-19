"""The weekly summary: Sundays at 18:00 (local), to every channel that asks for it.

What's in it depends on what the account has: per person, screen time and top apps and
sites (computers with Controls on), arrivals at places, devices that went quiet; for
applications, the week's uptime and incidents; for servers, the alerts raised. Sections
with nothing in them are left out.

Sent at most once per channel per week (Channel.last_report_at), and only on the Sunday
evening itself: a relay that was down for days doesn't send last week's report late.
"""

import html
import logging
from collections import Counter
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .channels import Message, SendError
from .models import Alert, AlertKind, Channel, Delivery, DeliveryEvent

logger = logging.getLogger(__name__)

REPORT_WEEKDAY = 6  # Sunday
REPORT_AT = time(18, 0)
SEND_WITHIN = timedelta(hours=6)


def last_boundary(now=None) -> datetime:
    """The most recent Sunday 18:00 (local) at or before `now`."""
    local = timezone.localtime(now or timezone.now())
    days_back = (local.weekday() - REPORT_WEEKDAY) % 7
    boundary = datetime.combine(local.date() - timedelta(days=days_back), REPORT_AT,
                                tzinfo=local.tzinfo)  # fmt: skip
    if boundary > local:
        boundary -= timedelta(days=7)
    return boundary


def _hours(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _people_lines(account, start, end) -> list[str]:
    from apps.devices.models import Device, ScreenTime, WebActivity
    from apps.tenants.models import Person

    lines = []
    first, last = start.date(), (end - timedelta(seconds=1)).date()
    arrivals = Counter()
    for device_person in Alert.objects.filter(
        account=account, kind=AlertKind.PLACE, is_event=True, opened_at__gte=start,
        opened_at__lt=end, subject__contains=":in:",
    ).values_list("device__person", flat=True):  # fmt: skip
        arrivals[device_person] += 1
    for person in Person.objects.filter(account=account).order_by("name"):
        devices = Device.objects.filter(person=person)
        if not devices.exists():
            continue
        parts = []
        screen = ScreenTime.objects.filter(device__in=devices, day__range=(first, last))
        total = screen.aggregate(s=Sum("seconds"))["s"] or 0
        if total:
            apps = screen.values("app").annotate(s=Sum("seconds")).order_by("-s")[:3]
            per_day = _hours(total // 7)
            parts.append(f"screen time {_hours(total)} (about {per_day} a day); most on "
                         + ", ".join(a["app"] for a in apps))  # fmt: skip
        sites = (WebActivity.objects.filter(device__in=devices, day__range=(first, last))
                 .values("domain").annotate(s=Sum("seconds")).order_by("-s")[:3])  # fmt: skip
        if sites:
            parts.append("top sites " + ", ".join(s["domain"] for s in sites))
        if arrivals[person.pk]:
            n = arrivals[person.pk]
            parts.append(f"{n} arrival{'s' if n != 1 else ''} at your places")
        if parts:
            lines.append(f"{person.name}: " + "; ".join(parts) + ".")
    quiet = Device.objects.filter(
        alerts__kind=AlertKind.DEVICE_OFFLINE, alerts__resolved_at=None, account=account
    ).distinct()  # fmt: skip
    for device in quiet:
        lines.append(f"{device.name} isn't reporting.")
    return lines


def _app_lines(account, start, end) -> list[str]:
    from apps.monitors.models import Monitor
    from apps.monitors.stats import uptime_by_monitor

    monitors = {m.pk: m.name for m in Monitor.objects.filter(account=account)}
    if not monitors:
        return []
    uptime = uptime_by_monitor(list(monitors), hours=24 * 7)
    incidents = Counter(Alert.objects.filter(
        account=account, kind=AlertKind.APP_DOWN, opened_at__gte=start, opened_at__lt=end
    ).values_list("monitor_id", flat=True))  # fmt: skip
    lines = []
    worst = sorted(uptime.items(), key=lambda kv: kv[1])
    if worst and all(pct >= 99.95 for _, pct in worst):
        lines.append(f"All {len(worst)} applications were up all week.")
    else:
        for pk, pct in worst[:5]:
            n = incidents[pk]
            lines.append(f"{monitors[pk]}: {pct:.2f}% up"
                         + (f", {n} incident{'s' if n != 1 else ''}" if n else "") + ".")  # fmt: skip
    return lines


def _server_lines(account, start, end) -> list[str]:
    kinds = Counter(Alert.objects.filter(
        account=account, server__isnull=False, opened_at__gte=start, opened_at__lt=end
    ).values_list("kind", flat=True))  # fmt: skip
    if not kinds:
        return []
    labels = dict(AlertKind.choices)
    return ["Server alerts: " + ", ".join(f"{n} × {labels.get(k, k).lower()}"
                                          for k, n in kinds.most_common()) + "."]  # fmt: skip


def compose(account, boundary: datetime) -> Message | None:
    start, end = boundary - timedelta(days=7), boundary
    sections = []
    from apps.tenants.roles import has_feature

    if has_feature(account.kind, "devices"):
        if lines := _people_lines(account, start, end):
            sections.append((account.people_label, lines))
    if has_feature(account.kind, "monitors"):
        if lines := _app_lines(account, start, end):
            sections.append(("Applications", lines))
    if has_feature(account.kind, "servers"):
        if lines := _server_lines(account, start, end):
            sections.append(("Servers", lines))
    title = f"Your week in {settings.SITE_NAME}"
    span = f"{timezone.localtime(start):%b %-d} – {timezone.localtime(end):%b %-d}"
    if not sections:
        sections.append(("", ["A quiet week: nothing to report."]))
    plain = [f"📊 {title} · {span}"]
    rich = [f"<b>📊 {html.escape(title)}</b> · {html.escape(span)}"]
    for heading, lines in sections:
        if heading:
            plain.append(f"\n{heading}")
            rich.append(f"\n<b>{html.escape(heading)}</b>")
        plain.extend(f"• {line}" for line in lines)
        rich.extend(f"• {html.escape(line)}" for line in lines)
    url = settings.DASHBOARD_URL
    if url:
        plain.append(url)
    body = "\n".join(plain[1:])
    return Message(title=f"{title} · {span}", body=body, plain="\n".join(plain),
                   html="\n".join(rich), url=url, severity="info", event="report")  # fmt: skip


def process(now=None) -> int:
    """Send this week's report to channels still owed it. Returns how many went out."""
    from . import delivery

    now = now or timezone.now()
    boundary = last_boundary(now)
    if now - boundary > SEND_WITHIN:
        return 0
    channels = list(
        Channel.objects.filter(is_enabled=True, weekly_report=True, created_at__lt=boundary)
        .exclude(last_report_at__gte=boundary).select_related("account")
    )  # fmt: skip
    composed, sent = {}, 0
    for channel in channels:
        # Claim it first: two relay processes never both send.
        if not Channel.objects.filter(pk=channel.pk).exclude(last_report_at__gte=boundary
                                                             ).update(last_report_at=now):  # fmt: skip
            continue
        if channel.account_id not in composed:
            composed[channel.account_id] = compose(channel.account, boundary)
        record = Delivery(channel=channel, event=DeliveryEvent.REPORT, attempts=1)
        try:
            delivery.send(channel, composed[channel.account_id])
            record.ok, record.sent_at = True, now
            sent += 1
        except SendError as exc:
            record.error = str(exc)[:300]
            logger.warning("Weekly report to %s failed: %s", channel.name, exc)
        record.save()
    return sent
