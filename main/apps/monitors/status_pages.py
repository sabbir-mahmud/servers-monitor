"""Public status pages: what the page shows, worked out from monitors, their daily uptime,
their app_down alerts (incidents) and maintenance windows. Only names and states leave the
dashboard: never URLs, hosts, error messages or which server checks what."""

from datetime import timedelta

from django.utils import timezone

from apps.alerts.models import Alert, AlertKind, MaintenanceWindow

from .models import MonitorStatus, StatusPage
from .stats import daily_uptime

DAYS = 90
INCIDENT_DAYS = 14


def _bar(days: dict, today) -> tuple[list[dict], float | None]:
    bar, up_sum, total_sum = [], 0, 0
    for offset in range(DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        up, total = days.get(day, (0, 0))
        up_sum, total_sum = up_sum + up, total_sum + total
        pct = round(100.0 * up / total, 2) if total else None
        state = ("none" if pct is None else "up" if pct >= 99.9 else "partial" if pct >= 95
                 else "down")  # fmt: skip
        bar.append({"day": day, "pct": pct, "state": state})
    return bar, (round(100.0 * up_sum / total_sum, 2) if total_sum else None)


def context(page: StatusPage, now=None) -> dict:
    now = now or timezone.now()
    today = timezone.localtime(now).date()
    monitors = list(page.monitors.all().order_by("name"))
    ids = [m.pk for m in monitors]
    uptime = daily_uptime(ids, DAYS)
    rows = []
    for m in monitors:
        bar, pct = _bar(uptime.get(m.pk, {}), today)
        state = {MonitorStatus.DOWN: "down", MonitorStatus.DEGRADED: "slow",
                 MonitorStatus.PAUSED: "paused", MonitorStatus.PENDING: "pending"
                 }.get(m.status, "up")  # fmt: skip
        rows.append({"name": m.name, "state": state, "bar": bar, "uptime": pct})
    down = sum(1 for r in rows if r["state"] == "down")
    slow = sum(1 for r in rows if r["state"] == "slow")
    windows = [
        w for w in MaintenanceWindow.objects.filter(
            account_id=page.account_id, ends_at__gt=now, starts_at__lt=now + timedelta(days=7)
        ).prefetch_related("monitors").order_by("starts_at")
        if w.all_targets or {m.pk for m in w.monitors.all()} & set(ids)
    ]  # fmt: skip
    incidents = []
    if page.show_incidents:
        names = {m.pk: m.name for m in monitors}
        for a in Alert.objects.filter(
            monitor_id__in=ids, kind=AlertKind.APP_DOWN,
            opened_at__gte=now - timedelta(days=INCIDENT_DAYS),
        ).order_by("-opened_at")[:30]:  # fmt: skip
            end = a.resolved_at or now
            incidents.append({"name": names[a.monitor_id], "opened_at": a.opened_at,
                              "resolved_at": a.resolved_at, "minutes": int((end - a.opened_at)
                                                                           .total_seconds() // 60)})  # fmt: skip
    in_maintenance = any(w.starts_at <= now for w in windows)
    overall = ("down" if down else "slow" if slow else "maintenance" if in_maintenance
               else "up")  # fmt: skip
    return {
        "page": page,
        "rows": rows,
        "overall": overall,
        "down": down,
        "windows": windows,
        "incidents": incidents,
        "now": now,
        "days": DAYS,
    }
