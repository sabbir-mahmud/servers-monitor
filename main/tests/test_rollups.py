"""4.2: hourly server rollups, daily monitor uptime, long charts, per-mount forecasts."""

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from apps.alerts import forecast
from apps.alerts.models import Alert, AlertKind
from apps.fleet import rollups, services
from apps.fleet.models import MetricRollup, MetricSample, Server
from apps.monitors import stats
from apps.monitors.models import Monitor, MonitorCheck, MonitorDaily

from .conftest import make_server

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 10, 11, 12, 20, tzinfo=UTC)


def sample(server, at, cpu=10.0, disk=50.0):
    MetricSample.objects.create(server=server, recorded_at=at, cpu_percent=cpu,
                                memory_percent=40.0, disk_percent=disk, load_1=0.5)  # fmt: skip


@pytest.fixture
def server(db):
    return make_server(disks=[{"mount": "/", "percent": 51.0}, {"mount": "/var", "percent": 80.0}])[
        0
    ]


def test_hourly_rollups_cover_complete_hours_once(server):
    for minutes, cpu in ((-130, 10), (-100, 30), (-70, 20), (-10, 99)):
        sample(server, NOW + timedelta(minutes=minutes), cpu=cpu)
    assert rollups.build_hourly(NOW) == 2  # 10:00 and 11:00; 12:00 is still filling up
    r10, r11 = MetricRollup.objects.order_by("hour")
    assert (r10.hour, r10.samples, r10.cpu_avg, r10.cpu_max) == (
        NOW.replace(hour=10, minute=0),
        2,
        20.0,
        30.0,
    )
    assert r11.cpu_avg == 20.0
    assert r10.disks == {} and r11.disks == {
        "/": 51.0,
        "/var": 80.0,
    }  # snapshot: the hour just ended
    assert rollups.build_hourly(NOW) == 0  # idempotent
    assert rollups.build_hourly(NOW + timedelta(hours=1)) == 1  # 12:00 once it's complete


def test_daily_uptime_outlives_raw_checks(settings):
    settings.TIME_ZONE = "UTC"
    m = Monitor.objects.create(name="site", url="https://example.com")
    old = timezone.now() - timedelta(days=30)
    for i, ok in enumerate((True, True, True, False)):
        MonitorCheck.objects.create(
            monitor=m, checked_at=old + timedelta(minutes=i), ok=ok, response_ms=100
        )
    MonitorCheck.objects.create(
        monitor=m, checked_at=old, ok=False, location="dashboard"
    )  # a raw location row
    MonitorCheck.objects.create(
        monitor=m, checked_at=timezone.now(), ok=True
    )  # today: not rolled up
    assert rollups.build_daily() == 1 and rollups.build_daily() == 0
    day = MonitorDaily.objects.get()
    assert (day.up, day.total, day.avg_ms) == (3, 4, 100.0)
    MonitorCheck.objects.filter(
        checked_at__lt=timezone.now() - timedelta(days=14)
    ).delete()  # retention
    history = stats.daily_uptime([m.pk], 90)[m.pk]
    assert history[old.date()] == (3, 4) and history[timezone.now().date()] == (1, 1)


def test_prune_keeps_rollup_retention(server, settings):
    settings.ROLLUP_RETENTION_DAYS = 400
    for days in (10, 401):
        MetricRollup.objects.create(server=server, hour=NOW - timedelta(days=days), samples=1, cpu_avg=1,
                                    cpu_max=1, memory_avg=1, memory_max=1, disk_avg=1, disk_max=1)  # fmt: skip
    assert rollups.prune(NOW) == 1 and MetricRollup.objects.count() == 1


def test_long_charts_read_rollups_and_fresh_samples(server):
    now = timezone.now()
    base = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    for d in range(1, 30):
        MetricRollup.objects.create(server=server, hour=base - timedelta(days=d), samples=30, cpu_avg=d,
                                    cpu_max=d, memory_avg=1, memory_max=1, disk_avg=1, disk_max=1)  # fmt: skip
    sample(server, now - timedelta(minutes=5), cpu=77)
    series = services.chart_series(server, "30d")
    assert len(series["cpu"]) == 30 and series["cpu"][-1][1] == 77.0 and series["cpu"][0][1] == 29.0
    assert services.chart_series(server, "24h")["cpu"] == [[series["cpu"][-1][0], 77.0]]  # raw only


def test_forecast_names_the_mount_that_fills_first(server):
    now = timezone.now()
    for h in range(72):  # /var grows 0.2 points an hour; / is flat
        MetricRollup.objects.create(
            server=server, hour=now - timedelta(hours=72 - h), samples=30, cpu_avg=1, cpu_max=1,
            memory_avg=1, memory_max=1, disk_avg=50, disk_max=50,
            disks={"/": 50.0, "/var": 60.0 + 0.2 * h},  # 74.2 % now
        )  # fmt: skip
    when, mount = forecast.soonest(server, now)
    assert mount == "/var" and timedelta(hours=128) < when - now < timedelta(hours=130)
    forecast.update(server, now)
    alert = Alert.objects.get(kind=AlertKind.DISK_FORECAST)
    assert alert.message.startswith("/var on web-01 will be full in about 5 days")
    assert Server.objects.get(pk=server.pk).disk_full_at == when
