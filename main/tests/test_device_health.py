"""Device health: CPU, memory and GPU from computers, and the History charts."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.devices import services
from apps.devices.models import Device, DeviceSample

from . import test_devices
from .phones import pair_phone
from .test_devices import add_device, api_checkin, api_pair

pytestmark = pytest.mark.django_db

family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

HEALTH = {
    "cpu": {"percent": 37.25},
    "memory": {"total": 16 * 2**30, "used": 12 * 2**30},
    "gpu": {"percent": 18},
    "battery": {"percent": 64, "charging": False},
    "storage": {"total": 500 * 10**9, "free": 125 * 10**9},
}


@pytest.fixture
def laptop(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    token = api_pair(client, code, platform="macos").json()["token"]
    return device, token


def test_a_computer_reports_its_health_and_it_is_kept_as_history(client, laptop):
    device, token = laptop
    assert api_checkin(client, token, HEALTH).status_code == 200
    device.refresh_from_db()
    assert device.cpu_percent == 37.2 and device.gpu_percent == 18
    assert device.memory_used_percent == 75
    sample = device.samples.get()
    assert (sample.cpu_percent, sample.memory_percent, sample.gpu_percent) == (37.2, 75, 18)
    assert (sample.battery_percent, sample.disk_percent) == (64, 75)


def test_nonsense_health_is_dropped_and_a_missing_reading_clears_the_old_one(client, laptop):
    device, token = laptop
    api_checkin(client, token, HEALTH)
    api_checkin(client, token, {"cpu": {"percent": 140}, "gpu": {"percent": True},
                                "memory": {"total": 8, "used": 9}})  # fmt: skip
    device.refresh_from_db()
    # Yesterday's "CPU 37%" must not be shown as if it were now.
    assert device.cpu_percent is None and device.gpu_percent is None
    assert device.memory_total is None and not device.has_health


def test_phones_never_store_computer_health(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)  # an Android phone
    device = pair_phone(device, code).checkin(HEALTH)  # through the relay, as the app does
    assert device.cpu_percent is None and device.memory_total is None
    assert device.samples.get().battery_percent == 64  # battery history still kept


def test_chart_series_averages_buckets_and_skips_gaps(laptop):
    device, _ = laptop
    now = timezone.now()
    for seconds, cpu, gpu in ((3510, 10, None), (3505, 30, None), (300, 50, 40)):
        DeviceSample.objects.create(device=device, recorded_at=now - timedelta(seconds=seconds),
                                    cpu_percent=cpu, gpu_percent=gpu)  # fmt: skip
    charts = services.chart_series(device, "6h")
    assert [v for _, v in charts["cpu"]] == [20.0, 50.0]  # 5 s apart: one bucket
    assert [v for _, v in charts["gpu"]] == [40.0] and charts["battery"] == []
    assert charts["data_from"] is not None  # recording began inside the range


def test_device_page_shows_health_now_and_switches_range(client, laptop):
    device, token = laptop
    api_checkin(client, token, HEALTH)
    url = reverse("devices:detail", args=[device.pk])
    page = client.get(url).content.decode()
    assert 'id="charts"' in page and "<dt>Memory</dt><dd>16.0\xa0GB</dd>" in page
    for label in ("CPU", "GPU"):
        assert page.count(f'class="spark-title">{label}<') == 1, label  # one chart each
    # The latest reading sits in the "right now" bar, so it shows from the first check-in
    # (a chart needs two buckets of data before it draws a line).
    assert 'aria-label="Health at the last check-in"' in page
    assert ">37%<" in page and ">75%<" in page and ">18%<" in page
    part = client.get(url, {"part": "charts", "range": "7d"}, HTTP_HX_REQUEST="true")
    body = part.content.decode()
    assert body.lstrip().startswith('<section id="charts"') and "range=7d" in body
    assert "About this device" not in body


def test_consent_names_the_health_data_for_computers(client, parent, sara):
    client.force_login(parent)
    _, code = add_device(client, sara, platform="windows", name="Family PC")
    disclosure = api_pair(client, code, platform="windows", preview=True).json()["disclosure"]
    assert "CPU, memory, GPU, battery and storage use" in disclosure
    assert "network (Wi-Fi name)" in disclosure
    _, code = add_device(client, sara, platform="macos", name="Sara's Mac")
    disclosure = api_pair(client, code, platform="macos", preview=True).json()["disclosure"]
    assert "network (Wi-Fi name)" in disclosure and "location" not in disclosure


def test_health_bar_is_hidden_until_a_computer_reports_health(client, laptop):
    device, _ = laptop
    page = client.get(reverse("devices:detail", args=[device.pk])).content.decode()
    assert "Health at the last check-in" not in page


def test_old_health_history_is_pruned(laptop, settings):
    device, _ = laptop
    settings.DEVICE_METRICS_DAYS = 30
    old = timezone.now() - timedelta(days=31)
    DeviceSample.objects.create(device=device, recorded_at=old, cpu_percent=5)
    DeviceSample.objects.create(device=device, recorded_at=timezone.now(), cpu_percent=6)
    assert services.prune_samples() == 1 and device.samples.count() == 1
    Device.objects.filter(pk=device.pk).delete()
    assert not DeviceSample.objects.exists()  # removed with the device
