"""Android phones (app 1.2+) get what computers get, through the relay only: signed
settings with screen time, websites and blocking at `:cfg`, a beep command at `:cmd`, and
an immediate alert when a protection is turned off on the phone."""

import json

import pytest

from apps.agent_api import relay, signing
from apps.alerts.models import Alert
from apps.devices import services
from apps.devices.models import Device, DeviceControls, phone_has_controls

from . import test_devices
from .phones import pair_phone
from .test_devices import add_device

family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara
solo_dev = test_devices.solo_dev

pytestmark = pytest.mark.django_db
NEW_APP = {"app_version": "nexguard-phone 1.2.0"}


def paired(client, parent, sara, platform="android", **info):
    client.force_login(parent)
    name = "Sara's phone" if platform == "android" else "Sara's iPhone"
    device, code = add_device(client, sara, platform=platform, name=name)
    phone = pair_phone(device, code, platform=platform, **{**NEW_APP, **info})
    return Device.objects.select_related("account", "person").get(pk=device.pk), phone


def read_signed(fake, phone, key) -> dict | None:
    """GET a value the dashboard left, as the app does: only if it's signed with its key."""
    raw = fake.strings.get(key)
    if raw is None:
        return None
    sealed = json.loads(raw)
    assert signing.sign_text(phone.key, sealed["body"]) == sealed["sig"], "unsigned"
    return json.loads(sealed["body"])


def test_app_versions():
    assert phone_has_controls("nexguard-phone 1.2.0") and phone_has_controls("nexguard-phone 2.0")
    assert not phone_has_controls("nexguard-phone 1.1.0")
    assert not phone_has_controls("nexguard-phone 1.2.0-nocontrols")  # Expo Go
    assert not phone_has_controls("nexguard-agent 1.2.0") and not phone_has_controls("")


def test_a_childs_android_phone_can_have_controls_but_not_an_iphone(client, parent, sara):
    android, _ = paired(client, parent, sara)
    assert android.controls_allowed and android.can_block and android.can_beep
    iphone, _ = paired(client, parent, sara, platform="ios", model="iPhone 17")
    assert iphone.controls_block_reason == "Screen time, websites and blocking are Android only."
    assert not iphone.can_beep


def test_an_older_app_is_asked_to_update(client, parent, sara):
    device, _ = paired(client, parent, sara, app_version="nexguard-phone 1.1.0")
    assert "update the app" in device.controls_block_reason and not device.can_beep


def test_the_preview_says_whether_controls_will_be_possible(client, parent, sara, fake_relay):
    from .test_phone_relay import try_code

    client.force_login(parent)
    device, code = add_device(client, sara)
    import secrets

    from apps.agent_api.bridge import handle_pairing

    from .phones import INFO, answer, pair_request

    def preview(**info):
        nonce = secrets.token_hex(12)
        request = json.loads(pair_request(code, "", nonce, {**INFO, **info})["body"])
        request.pop("secret")
        request["preview"] = True
        handle_pairing({"t": "pair", "body": json.dumps(request)})
        return answer(fake_relay, nonce)

    new = preview(**NEW_APP)
    assert new["state"] == "preview" and new["controls"] is True
    assert "making this phone ring" in new["disclosure"] and "uninstalled" in new["disclosure"]
    old = preview(app_version="nexguard-phone 1.1.0")
    assert old["controls"] is False and "ring" not in old["disclosure"]
    assert try_code(fake_relay, code)["state"] == "paired"  # the previews didn't use the code


def test_settings_are_left_signed_with_lists_inline(client, parent, sara, fake_relay):
    device, phone = paired(client, parent, sara)
    key = relay.device_config_key(device.pk)
    body = read_signed(fake_relay, phone, key)
    assert body["controls"]["screen_time"] is False and body["controls"]["blocked"] == []
    DeviceControls.objects.update_or_create(device=device, defaults={
        "screen_time": True, "web_activity": True, "block_adult": True, "safe_search": True,
        "blocked_domains": "tiktok.com\nhttps://www.reddit.com/r/x", "allowed_domains": "google.com"})  # fmt: skip
    services.publish_settings(device)
    c = read_signed(fake_relay, phone, key)["controls"]
    assert c["screen_time"] and c["web_activity"] and c["block_adult"]
    assert "tiktok.com" in c["blocked"] and "use-application-dns.net" in c["blocked"]
    assert c["allowed"] == ["google.com"]
    assert "google.com" not in c["safe_search"]["forcesafesearch.google.com"]  # allowed wins
    assert "www.google.com" in c["safe_search"]["forcesafesearch.google.com"]
    assert relay.device_blocklist_key(device.pk) not in fake_relay.strings  # no megabytes


def test_unchanged_settings_are_not_rewritten(client, parent, sara, fake_relay, monkeypatch):
    device, phone = paired(client, parent, sara)
    digest = relay.config_digest(services.settings_body(device))
    writes = []
    monkeypatch.setattr(relay, "publish_config", lambda d, b: writes.append(b))
    phone.checkin({**NEW_APP, "config": digest, "battery": {"percent": 80, "charging": False}})
    assert writes == []
    phone.checkin({**NEW_APP, "config": "stale", "battery": {"percent": 80, "charging": False}})
    assert len(writes) == 1


def test_a_phone_paired_before_gets_read_access_once(client, parent, sara, fake_relay):
    device, phone = paired(client, parent, sara, app_version="nexguard-phone 1.1.0")
    user = f"sm-d-{device.pk}"
    fake_relay.users[user] = ["reset", "on", f"~sm:d:{device.pk}:in", "-@all", "+rpush", "+ping"]
    phone.checkin({**NEW_APP})  # the app was updated
    rules = fake_relay.users[user]
    assert "+get" in rules and f"~sm:d:{device.pk}:cmd" in rules and "reset" in rules[:1]
    assert read_signed(fake_relay, phone, relay.device_config_key(device.pk)) is not None


def test_beep(client, parent, sara, fake_relay):
    device, phone = paired(client, parent, sara)
    page = client.get(f"/devices/{device.pk}/").content.decode()
    assert f"/devices/{device.pk}/beep/" in page
    resp = client.post(f"/devices/{device.pk}/beep/")
    assert resp.status_code == 302
    key = relay.device_command_key(device.pk)
    command = read_signed(fake_relay, phone, key)
    assert command["kind"] == "beep" and len(command["id"]) == 32
    assert fake_relay.expiry[key] == relay.COMMAND_SECONDS
    from apps.audit.models import AuditLog

    assert AuditLog.objects.filter(action="device.beep", target=device.name).exists()


def test_an_iphone_cant_beep(client, parent, sara, fake_relay):
    device, _ = paired(client, parent, sara, platform="ios", model="iPhone 17")
    assert (
        f"/devices/{device.pk}/beep/" not in client.get(f"/devices/{device.pk}/").content.decode()
    )
    client.post(f"/devices/{device.pk}/beep/")
    assert relay.device_command_key(device.pk) not in fake_relay.strings


def test_turning_a_protection_off_alerts_at_once_and_keeps_the_readings(client, parent, sara):
    device, phone = paired(client, parent, sara)
    phone.checkin({**NEW_APP, "battery": {"percent": 64, "charging": True},
                   "network": {"type": "wifi"}})  # fmt: skip
    # Sent by the filter service the moment it happens, seconds after the last check-in.
    from apps.agent_api.bridge import handle_device

    handle_device(device, phone.message({"partial": True, "tamper": ["admin_off", "nonsense"]}))
    alert = Alert.objects.get(device=device, subject__startswith="admin_off")
    assert "removal protection was turned off" in alert.message and "Sara" in alert.message
    device.refresh_from_db()
    assert device.battery_percent == 64 and device.network == "wifi"


def test_phone_screen_time_and_websites_are_stored(client, parent, sara):
    import uuid

    from django.utils import timezone

    device, phone = paired(client, parent, sara)
    DeviceControls.objects.update_or_create(device=device,
                                            defaults={"screen_time": True, "web_activity": True})  # fmt: skip
    today = timezone.localdate().isoformat()
    phone.checkin({**NEW_APP, "activity": {
        "batch": str(uuid.uuid4()),
        "screen": [{"day": today, "user": "Phone", "app": "YouTube", "seconds": 1800}],
        "web": [{"day": today, "user": "Phone", "browser": "Chrome", "domain": "youtube.com",
                 "visits": 4, "seconds": 600}],
    }})  # fmt: skip
    assert device.screen_time.get().seconds == 1800
    assert device.web_activity.get().domain == "youtube.com"


def test_the_controls_page_works_for_an_android_phone(client, parent, sara):
    device, _ = paired(client, parent, sara)
    page = client.get(f"/devices/{device.pk}/controls/").content.decode()
    assert "Sara's phone" in page and "Cloudflare 1.1.1.3" in page and "Protected app" in page
