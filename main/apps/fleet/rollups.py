"""Long-term history: hourly server rollups and daily monitor uptime.

Built by the relay process every hour from raw samples and checks (kept 14 days), and kept
ROLLUP_RETENTION_DAYS (400). Building is idempotent: each run starts after the newest
rollup, only covers complete hours (or days), and a unique constraint makes a repeat a
no-op. A dashboard that was down catches up from whatever raw data is still there.
"""

from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from .models import MetricRollup, MetricSample, Server


def _hour_start(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def build_hourly(now=None) -> int:
    """Roll up every complete UTC hour not rolled up yet. Returns rows created."""
    now = now or timezone.now()
    end = _hour_start(now)  # the current hour is still filling up
    created = 0
    for server in Server.objects.only("pk", "disks"):
        last = MetricRollup.objects.filter(server=server).aggregate(h=Max("hour"))["h"]
        start = (
            last + timedelta(hours=1)
            if last
            else end - timedelta(days=settings.ROLLUP_RETENTION_DAYS)
        )
        if start >= end:
            continue
        rows = (
            MetricSample.objects.filter(server=server, recorded_at__gte=start, recorded_at__lt=end)
            .annotate(h=TruncHour("recorded_at", tzinfo=UTC))
            .values("h")
            .annotate(n=Count("id"), cpu_avg=Avg("cpu_percent"), cpu_max=Max("cpu_percent"),
                      memory_avg=Avg("memory_percent"), memory_max=Max("memory_percent"),
                      disk_avg=Avg("disk_percent"), disk_max=Max("disk_percent"), load_avg=Avg("load_1"))
            .order_by("h")
        )  # fmt: skip
        just_ended = end - timedelta(hours=1)
        # Per-mount use is only in the latest heartbeat: it belongs to the hour that just ended.
        mounts = {d["mount"]: d["percent"] for d in server.disks if d.get("percent") is not None}
        objs = [
            MetricRollup(server=server, hour=r["h"], samples=r["n"], cpu_avg=r["cpu_avg"],
                         cpu_max=r["cpu_max"], memory_avg=r["memory_avg"], memory_max=r["memory_max"],
                         disk_avg=r["disk_avg"], disk_max=r["disk_max"], load_avg=r["load_avg"],
                         disks=mounts if r["h"] == just_ended else {})
            for r in rows
        ]  # fmt: skip
        created += len(MetricRollup.objects.bulk_create(objs, ignore_conflicts=True))
    return created


def build_daily(now=None) -> int:
    """Roll up every complete local day of monitor outcomes. Returns rows created."""
    from apps.monitors.models import Monitor, MonitorCheck, MonitorDaily

    tz = timezone.get_current_timezone()
    today = timezone.localtime(now or timezone.now(), tz).date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=tz)
    created = 0
    for monitor in Monitor.objects.only("pk"):
        last = MonitorDaily.objects.filter(monitor=monitor).aggregate(d=Max("day"))["d"]
        since = (
            datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz)
            if last
            else day_start - timedelta(days=settings.ROLLUP_RETENTION_DAYS)
        )
        if since >= day_start:
            continue
        rows = (
            MonitorCheck.objects.filter(monitor=monitor, location="", checked_at__gte=since,
                                        checked_at__lt=day_start)
            .annotate(d=TruncDate("checked_at", tzinfo=tz))
            .values("d")
            .annotate(total=Count("id"), up=Count("id", filter=Q(ok=True)), avg_ms=Avg("response_ms"))
        )  # fmt: skip
        objs = [MonitorDaily(monitor=monitor, day=r["d"], up=r["up"], total=r["total"], avg_ms=r["avg_ms"])
                for r in rows]  # fmt: skip
        created += len(MonitorDaily.objects.bulk_create(objs, ignore_conflicts=True))
    return created


def prune(now=None) -> int:
    from apps.monitors.models import MonitorDaily

    cutoff = (now or timezone.now()) - timedelta(days=settings.ROLLUP_RETENTION_DAYS)
    deleted = MetricRollup.objects.filter(hour__lt=cutoff).delete()[0]
    return deleted + MonitorDaily.objects.filter(day__lt=cutoff.date()).delete()[0]


def build_all(now=None) -> dict:
    return {"hourly": build_hourly(now), "daily": build_daily(now), "pruned": prune(now)}
