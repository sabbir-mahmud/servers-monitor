"""1.5: disk-full forecast."""

import random
from datetime import timedelta

import pytest
from django.template import Context, Template
from django.utils import timezone

from apps.alerts import forecast
from apps.alerts.models import Alert, AlertKind, Severity
from apps.fleet.models import MetricSample

pytestmark = pytest.mark.django_db


def _history(server, now, per_hour, start=50.0, hours=96, noise=0.0, seed=1):
    rnd = random.Random(seed)
    MetricSample.objects.bulk_create(
        MetricSample(
            server=server,
            recorded_at=now - timedelta(hours=hours - h, minutes=m),
            cpu_percent=1,
            memory_percent=1,
            disk_percent=start + per_hour * h + rnd.uniform(-noise, noise),
        )
        for h in range(hours)
        for m in (0, 30)
    )


def test_fit_is_exact_for_a_line():
    a, b, r2 = forecast.fit([(x, 3 + 2 * x) for x in range(10)])
    assert (round(a, 6), round(b, 6), round(r2, 6)) == (3, 2, 1)


def test_linear_growth_predicts_within_5_percent(server):
    now = timezone.now()
    _history(server, now, per_hour=0.25)  # 6 %/day from 50 % → ~74 % now
    full_at = forecast.predict_full_at(server, now)
    expected_hours = (100 - (50 + 0.25 * 95)) / 0.25
    assert full_at is not None
    assert abs((full_at - now).total_seconds() / 3600 - expected_hours) <= expected_hours * 0.05


@pytest.mark.parametrize(
    "per_hour,noise",
    [(0.0, 0.5), (-0.1, 0.0), (0.02, 0.0), (0.1, 25.0)],  # flat, shrinking, too slow, too noisy
)
def test_no_forecast_without_clear_growth(server, per_hour, noise):
    now = timezone.now()
    _history(server, now, per_hour=per_hour, start=40, noise=noise)
    assert forecast.predict_full_at(server, now) is None


def test_too_little_history(server):
    now = timezone.now()
    _history(server, now, per_hour=0.5, hours=24)
    assert forecast.predict_full_at(server, now) is None


def test_alert_opens_escalates_and_resolves_with_hysteresis(server, monkeypatch):
    now = timezone.now()
    for days, kind in [(6, Severity.WARNING), (1.5, Severity.CRITICAL)]:
        monkeypatch.setattr(
            forecast, "predict_full_at", lambda s, n, d=days: now + timedelta(days=d)
        )
        forecast.update(server, now)
        alert = Alert.objects.get(kind=AlertKind.DISK_FORECAST, resolved_at=None)
        assert alert.severity == kind
    monkeypatch.setattr(forecast, "predict_full_at", lambda s, n: now + timedelta(days=8))
    forecast.update(server, now)  # 8 days: between 7 and 10 → stays open
    assert Alert.objects.filter(kind=AlertKind.DISK_FORECAST, resolved_at=None).exists()
    monkeypatch.setattr(forecast, "predict_full_at", lambda s, n: None)
    forecast.update(server, now)
    assert not Alert.objects.filter(kind=AlertKind.DISK_FORECAST, resolved_at=None).exists()
    server.refresh_from_db()
    assert server.disk_full_at is None


def test_fills_in_filter():
    t = Template("{% load monitor %}{{ v|fills_in }}")
    soon = timezone.now() + timedelta(days=6, hours=1)
    assert t.render(Context({"v": soon})) == "fills in ~6 days"
    assert (
        t.render(Context({"v": timezone.now() + timedelta(hours=5, minutes=5)})) == "fills in ~5 h"
    )
    assert t.render(Context({"v": timezone.now() + timedelta(days=60)})) == ""
