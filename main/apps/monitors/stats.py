"""Read-side helpers: uptime percentages, the status timeline and response-time series."""

from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import MonitorCheck

# Rows with a location are one agent's (or the dashboard's) own view of a multi-location
# monitor; the monitor's outcome (after the quorum) is the row without one.
OUTCOMES = Q(location="")

RANGES = {"1h": 1, "24h": 24, "7d": 24 * 7, "14d": 24 * 14}


def uptime_by_monitor(monitor_ids, hours: int = 24) -> dict:
    """{monitor_id: percent up} over the window; monitors without checks are absent."""
    since = timezone.now() - timedelta(hours=hours)
    rows = (
        MonitorCheck.objects.filter(OUTCOMES, monitor_id__in=monitor_ids, checked_at__gte=since)
        .values("monitor_id")
        .annotate(total=Count("id"), up=Count("id", filter=Q(ok=True)))
    )
    return {r["monitor_id"]: 100.0 * r["up"] / r["total"] for r in rows if r["total"]}


def avg_response_by_monitor(monitor_ids, hours: int = 24) -> dict:
    from django.db.models import Avg

    since = timezone.now() - timedelta(hours=hours)
    rows = (
        MonitorCheck.objects.filter(
            OUTCOMES, monitor_id__in=monitor_ids, checked_at__gte=since, response_ms__isnull=False
        )
        .values("monitor_id")
        .annotate(avg=Avg("response_ms"))
    )
    return {r["monitor_id"]: r["avg"] for r in rows}


def timelines(monitor_ids, hours: int = 24, buckets: int = 48) -> dict:
    """{monitor_id: [bucket, ...]} oldest first. Each bucket is a dict with `state` in
    none/up/partial/down plus its time span and counts, for the uptime bar."""
    now = timezone.now()
    start = now - timedelta(hours=hours)
    width = (now - start) / buckets
    counts = defaultdict(lambda: [[0, 0, 0] for _ in range(buckets)])
    for mid, at, ok, degraded in MonitorCheck.objects.filter(
        OUTCOMES, monitor_id__in=monitor_ids, checked_at__gte=start
    ).values_list("monitor_id", "checked_at", "ok", "degraded"):
        i = min(buckets - 1, int((at - start) / width))
        counts[mid][i][0 if ok else 1] += 1
        if ok and degraded:
            counts[mid][i][2] += 1
    result = {}
    for mid in monitor_ids:
        row = []
        for i, (up, down, slow) in enumerate(counts[mid]):
            state = (
                "none" if up + down == 0
                else ("slow" if slow else "up") if not down
                else "down" if not up
                else "partial"
            )  # fmt: skip
            row.append(
                {
                    "state": state,
                    "start": start + width * i,
                    "end": start + width * (i + 1),
                    "up": up,
                    "down": down,
                    "slow": slow,
                    "pct": round(100.0 * up / (up + down), 1) if up + down else None,
                }
            )
        result[mid] = row
    return result


def response_series(monitor, range_key: str) -> dict:
    hours = RANGES.get(range_key, 24)
    end = timezone.now()
    start = end - timedelta(hours=hours)
    rows = list(
        MonitorCheck.objects.filter(OUTCOMES, monitor=monitor, checked_at__gte=start)
        .order_by("checked_at")
        .values_list("checked_at", "response_ms")
    )
    # Long ranges: average into ~240 buckets so the page stays light.
    step = max(1, len(rows) // 240)
    points = []
    for i in range(0, len(rows), step):
        chunk = [ms for _, ms in rows[i : i + step] if ms is not None]
        if chunk:
            points.append((int(rows[i][0].timestamp()), sum(chunk) / len(chunk)))
    peak = max((v for _, v in points), default=0)
    ymax = next((n for n in (100, 250, 500, 1000, 2000, 5000, 10000, 30000) if peak <= n), 60000)
    return {
        "points": points,
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        "ymax": ymax,
    }


def incidents(monitor, limit: int = 20) -> list[dict]:
    """Up/down transitions from the check history, newest first."""
    rows = list(
        MonitorCheck.objects.filter(
            OUTCOMES, monitor=monitor, checked_at__gte=MonitorCheck.retention_cutoff()
        )
        .order_by("checked_at")
        .values_list("checked_at", "ok", "error", "status_code")
    )
    out, current = [], None
    for at, ok, error, code in rows:
        if current is None or current["ok"] != ok:
            if current is not None:
                current["ended"] = at
            current = {"ok": ok, "started": at, "ended": None, "error": error, "code": code}
            out.append(current)
    downs = [p for p in out if not p["ok"]]
    for d in downs:
        end = d["ended"] or timezone.now()
        d["duration"] = end - d["started"]
    return list(reversed(downs))[:limit]


def daily_uptime(monitor_ids, days: int = 90) -> dict:
    """{monitor_id: {date: (ok_checks, total_checks)}} for the last `days` local days: daily
    rollups for complete days (kept ROLLUP_RETENTION_DAYS), raw checks for the rest (today,
    or days the relay hasn't rolled up yet)."""
    from django.db.models.functions import TruncDate

    from .models import MonitorDaily

    tz = timezone.get_current_timezone()
    since = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )
    result = defaultdict(dict)
    for mid, day, up, total in MonitorDaily.objects.filter(
        monitor_id__in=monitor_ids, day__gte=since.date()
    ).values_list("monitor_id", "day", "up", "total"):
        result[mid][day] = (up, total)
    rows = (
        MonitorCheck.objects.filter(OUTCOMES, monitor_id__in=monitor_ids, checked_at__gte=since)
        .annotate(day=TruncDate("checked_at", tzinfo=tz))
        .values("monitor_id", "day")
        .annotate(total=Count("id"), up=Count("id", filter=Q(ok=True)))
    )
    for r in rows:
        # A rolled-up day is complete; raw checks for it may already be partly pruned.
        result[r["monitor_id"]].setdefault(r["day"], (r["up"], r["total"]))
    return result
