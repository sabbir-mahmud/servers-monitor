"""Tamper alerts: uninstalled, unpaired on the device, blocking edited, rights dropped,
location turned off."""

import pytest

from apps.agent_api.bridge import handle_device
from apps.alerts.models import Alert, AlertKind
from apps.devices.models import Device

from . import test_devices
from .test_devices import add_device, api_checkin, api_pair
from .test_phone_relay import check_in, paired_phone

pytestmark = pytest.mark.django_db
family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara


@pytest.fixture
def computer(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="windows", name="Family PC")
    token = api_pair(client, code, platform="windows", agent_mode="system",
                     app_version="nexguard-agent/1.0").json()["token"]  # fmt: skip
    return Device.objects.get(pk=device.pk), token


def long_ago():
    """Past the bridge's 30 s check-in throttle."""
    from datetime import timedelta

    from django.utils import timezone

    def push_back(device):
        Device.objects.filter(pk=device.pk).update(last_seen_at=timezone.now() - timedelta(hours=1))
        device.refresh_from_db()

    return push_back


def tamper(device, **kw):
    return Alert.objects.filter(device=device, kind=AlertKind.TAMPER, **kw)


def test_uninstall_alerts_without_wiping_readings_and_returning_resolves(client, computer):
    device, token = computer
    api_checkin(client, token, {"battery": {"percent": 80, "charging": True}})
    assert api_checkin(client, token, {"bye": "uninstall"}).status_code == 200
    alert = tamper(device, resolved_at=None).get()
    assert alert.message == "NexGuard was uninstalled from Family PC (Sara)"
    device.refresh_from_db()
    assert device.battery_percent == 80
    api_checkin(client, token, {})
    assert not tamper(device, resolved_at=None).exists()


def test_edited_blocklist_is_an_event(client, computer):
    device, token = computer
    api_checkin(client, token, {"tamper": ["hosts_edited", "nonsense"]})
    events = list(tamper(device, is_event=True).values_list("message", flat=True))
    assert events == ["Someone changed the blocked-sites list on Family PC (Sara); "
                      "NexGuard put it back"]  # fmt: skip


def test_dropping_to_user_mode_is_reported(client, computer):
    device, token = computer
    Device.objects.filter(pk=device.pk).update(agent_mode="system")
    api_checkin(client, token, {"agent_mode": "user"})
    assert "without administrator rights" in tamper(device, is_event=True).get().message


def test_phone_unpaired_on_itself_and_location_off(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    login = paired["relay"]
    long_ago()(device)
    handle_device(device, check_in(login, {"location": {"lat": 23.8, "lon": 90.4},
                                           "permissions": {"location": True}}))  # fmt: skip
    long_ago()(device)
    handle_device(device, check_in(login, {"permissions": {"location": False}}))
    assert tamper(device, subject="location", resolved_at=None).get().message == (
        "Location sharing was turned off on Sara's phone"
    )
    handle_device(device, check_in(login, {"bye": "unpaired"}))  # within 30 s: still applied
    assert tamper(device, subject="bye", resolved_at=None).get().message == (
        "Sara's phone was unpaired on the device itself"
    )


def test_location_off_before_ever_sharing_is_not_tampering(client, parent, sara):
    device, paired = paired_phone(client, parent, sara)
    handle_device(device, check_in(paired["relay"], {"permissions": {"location": False}}))
    assert not tamper(device).exists()
