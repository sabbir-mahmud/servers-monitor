"""Disk-full forecast: a straight line through the last week of hourly disk averages, for
the fullest disk (from raw samples) and for each mount (from hourly rollups, which keep a
per-mount snapshot); the soonest wins.

Only trusts clear, steady growth (enough points, a real slope, a good fit), so log rotation,
a one-off upload or noise doesn't cry wolf. Alerts with hysteresis: opens within 7 days
(critical within 2), resolves only once the forecast moves past 10 days or disappears.
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Avg
from django.db.models.functions import TruncHour
from django.utils import timezone

from apps.fleet.models import MetricSample, Server

from .models import AlertKind, Severity
from .services import _open, _open_alerts, _resolve

WINDOW = timedelta(days=7)
MIN_POINTS = 48  # two days of hourly averages
MIN_SLOPE = 0.05  # percentage points per hour (~1.2 %/day)
MIN_R2 = 0.6
WARN_WITHIN = timedelta(days=7)
CRITICAL_WITHIN = timedelta(days=2)
CLEAR_BEYOND = timedelta(days=10)


def fit(points: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    """Least squares y = a + b·x. Returns (a, b, r²), or None if x doesn't vary."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in points)
    syy = sum((y - my) ** 2 for _, y in points)
    b = sxy / sxx
    a = my - b * mx
    r2 = (sxy * sxy) / (sxx * syy) if syy else 0.0
    return a, b, r2


def predict_full_at(server: Server, now=None):
    """When the fullest disk (whichever it is) reaches 100 %, from raw samples."""
    now = now or timezone.now()
    rows = (
        MetricSample.objects.filter(server=server, recorded_at__gte=now - WINDOW)
        .annotate(hour=TruncHour("recorded_at"))
        .values("hour")
        .annotate(disk=Avg("disk_percent"))
        .order_by("hour")
    )
    return _extrapolate([((r["hour"] - now).total_seconds() / 3600, r["disk"]) for r in rows], now)


def predict_by_mount(server: Server, now=None) -> dict:
    """{mount: when it fills} for each mount with clear growth, from hourly rollups."""
    from apps.fleet.models import MetricRollup

    now = now or timezone.now()
    series: dict[str, list] = {}
    for hour, disks in (
        MetricRollup.objects.filter(server=server, hour__gte=now - WINDOW)
        .exclude(disks={})
        .order_by("hour")
        .values_list("hour", "disks")
    ):
        for mount, pct in disks.items():
            if isinstance(pct, int | float):
                series.setdefault(mount, []).append(((hour - now).total_seconds() / 3600, pct))
    out = {}
    for mount, points in series.items():
        if (when := _extrapolate(points, now)) is not None:
            out[mount] = when
    return out


def soonest(server: Server, now=None) -> tuple:
    """(when, mount or None): the first disk to fill, or (None, None)."""
    now = now or timezone.now()
    candidates = [(when, mount) for mount, when in predict_by_mount(server, now).items()]
    if (fullest := predict_full_at(server, now)) is not None:
        candidates.append((fullest, None))
    return min(candidates, key=lambda c: c[0]) if candidates else (None, None)


def _extrapolate(points: list[tuple[float, float]], now):
    if len(points) < MIN_POINTS:
        return None
    result = fit(points)
    if result is None:
        return None
    a, slope, r2 = result
    if slope < MIN_SLOPE or r2 < MIN_R2:
        return None
    current = points[-1][1]  # the latest hour's real value, not the line's
    if current >= 100:
        return now
    return now + timedelta(hours=(100 - current) / slope)


def update(server: Server, now=None) -> None:
    now = now or timezone.now()
    full_at, mount = soonest(server, now)
    Server.objects.filter(pk=server.pk).update(disk_full_at=full_at)
    with transaction.atomic():
        open_alerts = _open_alerts(server)
        left = full_at - now if full_at else None
        if left is not None and left <= WARN_WITHIN:
            days = left.total_seconds() / 86400
            when = (
                f"about {days:.0f} day{'s' if round(days) != 1 else ''}"
                if days >= 1
                else (f"about {left.total_seconds() / 3600:.0f} h")
            )
            severity = Severity.CRITICAL if left <= CRITICAL_WITHIN else Severity.WARNING
            disk = f"{mount} on {server.name}" if mount else f"{server.name}'s disk"
            _open(server, open_alerts, AlertKind.DISK_FORECAST,
                  f"{disk} will be full in {when} at the current rate", severity,
                  value=round(days, 1))  # fmt: skip
        elif left is None or left > CLEAR_BEYOND:
            _resolve(open_alerts, AlertKind.DISK_FORECAST)


def update_all(now=None) -> int:
    count = 0
    for server in Server.objects.filter(is_enabled=True, last_seen_at__isnull=False).only(
        "id", "name"
    ):
        update(server, now)
        count += 1
    return count
