"""The phone's SOS button, through the relay."""

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.agent_api.bridge import handle_device
from apps.alerts import delivery
from apps.alerts.models import Alert, AlertKind, Channel, Severity
from apps.alerts.services import sweep
from apps.devices.models import DeviceLocation

from . import test_devices
from .test_phone_relay import paired_phone

pytestmark = pytest.mark.django_db
family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

HERE = {"lat": 23.81, "lon": 90.41, "accuracy": 9, "source": "gps"}


def sos(login, location=HERE, cancel=False, at=None, key=None):
    body = json.dumps({"at": int(at or time.time()), "location": location, "cancel": cancel})
    sig = hmac.new(bytes.fromhex(key or login["key"]), body.encode(), hashlib.sha256).hexdigest()
    return {"t": "sos", "body": body, "sig": sig}


def open_sos(device):
    return Alert.objects.filter(device=device, kind=AlertKind.SOS, resolved_at=None)


def test_sos_opens_a_critical_alert_with_the_location(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login))
    alert = open_sos(device).get()
    assert alert.severity == Severity.CRITICAL
    assert alert.message.startswith("SOS from Sara at 23.81000, 90.41000 (±9 m)")
    assert "openstreetmap.org/?mlat=23.81000&mlon=90.41000" in alert.message
    assert DeviceLocation.objects.filter(device=device).count() == 1
    device.refresh_from_db()
    assert float(device.latitude) == 23.81


def test_sos_without_location_says_so(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login, location=None))
    assert open_sos(device).get().message == "SOS from Sara"


def test_im_ok_closes_it(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login))
    handle_device(device, sos(login, cancel=True))
    assert not open_sos(device).exists()


def test_forged_and_stale_sos_are_ignored(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login, key="00" * 32))
    handle_device(device, sos(login, at=time.time() - 3600))
    assert not Alert.objects.exists()


def test_sos_is_sent_even_to_critical_only_channels_and_repeats(
    client,
    parent,
    sara,
    family,
    monkeypatch,
):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    sent = []
    monkeypatch.setattr(delivery, "send", lambda c, m, *_: sent.append(m.plain))
    c = Channel.objects.create(account=family, name="mum", kind="telegram", telegram_token="t",
                               telegram_chat_id="1", min_severity="critical",
                               repeat_minutes=5)  # fmt: skip
    Channel.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(hours=1))
    handle_device(device, sos(login))
    assert delivery.process() == 1 and "CRITICAL · SOS" in sent[0]
    assert delivery.process(now=timezone.now() + timedelta(minutes=6)) == 1  # reminder


def test_pressed_again_after_acknowledgement_is_a_new_alert(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login))
    first = open_sos(device).get()
    first.acknowledged_at = timezone.now()
    first.save()
    handle_device(device, sos(login))
    assert open_sos(device).get().pk != first.pk


def test_acknowledged_sos_closes_after_an_hour(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired.login
    handle_device(device, sos(login))
    open_sos(device).update(acknowledged_at=timezone.now() - timedelta(hours=2))
    sweep()
    assert not open_sos(device).exists()
