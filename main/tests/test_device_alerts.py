"""Device alerts: not reporting, battery low, and how they're routed and announced."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.alerts import delivery, device_rules
from apps.alerts.models import Alert, AlertKind, Channel, Severity
from apps.alerts.services import sweep
from apps.devices import services
from apps.devices.models import Device

from . import test_devices
from .test_devices import add_device, api_checkin, api_pair

pytestmark = pytest.mark.django_db
family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara


@pytest.fixture
def phone(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    token = api_pair(client, code).json()["token"]
    return Device.objects.get(pk=device.pk), token


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(delivery, "send", lambda channel, message, *_: sent.append(
        (channel.name, message.plain)))  # fmt: skip
    return sent


def channel(account, **kw):
    c = Channel.objects.create(account=account, name=kw.pop("name", "mum"), kind="telegram",
                               telegram_token="t", telegram_chat_id="1", **kw)  # fmt: skip
    Channel.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(hours=1))
    return c


def open_alerts(device, kind):
    return Alert.objects.filter(device=device, kind=kind, resolved_at=None)


def test_low_battery_opens_then_resolves_when_charging(client, phone):
    device, token = phone
    api_checkin(client, token, {"battery": {"percent": 12, "charging": False}})
    alert = open_alerts(device, AlertKind.BATTERY).get()
    assert alert.severity == Severity.WARNING and alert.message.startswith("Battery at 12%")
    assert alert.account_id == device.account_id

    api_checkin(client, token, {"battery": {"percent": 4, "charging": False}})
    assert open_alerts(device, AlertKind.BATTERY).get().severity == Severity.CRITICAL

    api_checkin(client, token, {"battery": {"percent": 5, "charging": True}})
    assert not open_alerts(device, AlertKind.BATTERY).exists()


def test_battery_hysteresis(client, phone):
    device, token = phone
    api_checkin(client, token, {"battery": {"percent": 15, "charging": False}})
    api_checkin(client, token, {"battery": {"percent": 18, "charging": False}})
    assert open_alerts(device, AlertKind.BATTERY).exists()  # 18 is within 5 of the mark
    api_checkin(client, token, {"battery": {"percent": 21, "charging": False}})
    assert not open_alerts(device, AlertKind.BATTERY).exists()


def test_silent_device_opens_not_reporting_once_and_checkin_resolves(client, phone, settings):
    device, token = phone
    settings.DEVICE_OFFLINE_ALERT_HOURS = 4
    api_checkin(client, token, {})
    Device.objects.filter(pk=device.pk).update(last_seen_at=timezone.now() - timedelta(hours=5))
    assert sweep()["devices_silent"] == 1
    assert sweep()["devices_silent"] == 0  # already open
    alert = open_alerts(device, AlertKind.DEVICE_OFFLINE).get()
    assert alert.message == "Sara's phone hasn't reported for 5 h"
    api_checkin(client, token, {})
    assert not open_alerts(device, AlertKind.DEVICE_OFFLINE).exists()


def test_unpaired_and_recent_devices_never_alert(client, parent, sara):
    client.force_login(parent)
    add_device(client, sara, name="Waiting phone")
    assert sweep()["devices_silent"] == 0


def test_device_alerts_are_announced_with_a_link(client, phone, outbox, settings, family):
    device, token = phone
    settings.DASHBOARD_URL = "https://watch.example"
    channel(family)
    channel(family, name="servers-only", include_devices=False)
    api_checkin(client, token, {"battery": {"percent": 9, "charging": False}})
    assert delivery.process() == 1
    name, text = outbox[0]
    assert name == "mum" and "Battery low" in text and "Sara's phone · Sara" in text
    assert f"https://watch.example/devices/{device.pk}/" in text


def test_other_accounts_never_hear_about_a_device(client, phone, outbox, account):
    device, token = phone
    channel(account, name="acme")
    api_checkin(client, token, {"battery": {"percent": 9, "charging": False}})
    assert delivery.process() == 0


def test_deleting_a_device_removes_its_alerts(client, phone):
    device, token = phone
    api_checkin(client, token, {"battery": {"percent": 9, "charging": False}})
    device.delete()
    assert not Alert.objects.exists()


def test_alert_list_and_api_show_the_device(client, phone, parent):
    device, token = phone
    api_checkin(client, token, {"battery": {"percent": 9, "charging": False}})
    page = client.get(reverse("alerts:list")).content.decode()
    assert "Battery at 9%" in page and reverse("devices:detail", args=[device.pk]) in page


def test_open_alert_helper_is_idempotent(phone):
    device, _ = phone
    a = device_rules.open_alert(device, AlertKind.SOS, "SOS", Severity.CRITICAL)
    b = device_rules.open_alert(device, AlertKind.SOS, "SOS", Severity.CRITICAL)
    assert a.pk == b.pk
    assert device_rules.resolve_alert(device, AlertKind.SOS)


def test_channel_form_requires_some_kind(client, phone, family):
    resp = client.post(reverse("alerts:channel_create") + "?kind=telegram", {
        "kind": "telegram", "name": "x", "telegram_token": "123:abc", "telegram_chat_id": "1",
        "min_severity": "warning", "repeat_minutes": 0, "is_enabled": "on",
    })  # fmt: skip
    assert resp.status_code == 200 and "Choose at least one kind of alert" in resp.content.decode()


def test_services_checkin_runs_rules(phone):
    device, _ = phone
    services.checkin(device, {"battery": {"percent": 3, "charging": False}}, None)
    assert open_alerts(device, AlertKind.BATTERY).get().severity == Severity.CRITICAL
