"""Phones check in through the Redis relay (Upstash REST), not to the dashboard: pairing
hands each one its own Redis login and signing key (apps/agent_api/relay.py)."""

import hashlib
import hmac
import json
import time

import pytest

from apps.agent_api import relay
from apps.agent_api.bridge import handle_device
from apps.devices.models import Device

from . import test_devices
from .test_devices import add_device, api_pair

family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara

pytestmark = pytest.mark.django_db


def paired_phone(client, parent, sara, name="Sara's phone"):
    client.force_login(parent)
    device, code = add_device(client, sara, name=name)
    client.logout()
    resp = api_pair(client, code)
    assert resp.status_code == 201, resp.content
    device.refresh_from_db()
    return device, resp.json()


def check_in(login: dict, report: dict, at: float | None = None) -> dict:
    """What the phone app RPUSHes: the body text, signed with its key."""
    body = json.dumps({"at": int(at if at is not None else time.time()), "report": report})
    sig = hmac.new(bytes.fromhex(login["key"]), body.encode(), hashlib.sha256).hexdigest()
    return {"t": "ci", "body": body, "sig": sig}


def test_pairing_a_phone_gives_it_its_own_relay_login(client, parent, sara, fake_relay):
    device, answer = paired_phone(client, parent, sara)
    login = answer["relay"]
    assert login["url"] == "https://relay.test"
    assert login["inbox"] == f"sm:d:{device.pk}:in" and login["token"].startswith("rest_")
    assert len(bytes.fromhex(login["key"])) == 32 and device.relay_salt
    rules = fake_relay.users[f"sm-d-{device.pk}"]
    assert f"~sm:d:{device.pk}:in" in rules and "-@all" in rules
    assert {r for r in rules if r.startswith("+")} == {"+rpush", "+ping"}  # push only


def test_a_computer_gets_no_relay_login(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara, platform="windows", name="PC")
    resp = api_pair(client, code, platform="windows")
    assert resp.status_code == 201 and "relay" not in resp.json()


def test_without_upstash_phones_cant_pair_and_the_code_still_works(client, parent, sara, settings):
    settings.RELAY_REST_URL = ""  # REDIS_URL is relay.test: no REST API
    client.force_login(parent)
    device, code = add_device(client, sara)
    resp = api_pair(client, code)
    assert resp.status_code == 503 and "Upstash" in resp.json()["error"]
    settings.RELAY_REST_URL = "https://relay.test"
    assert api_pair(client, code).status_code == 201


def test_a_signed_check_in_through_the_relay_updates_the_phone(client, parent, sara):
    device, answer = paired_phone(client, parent, sara)
    Device.objects.filter(pk=device.pk).update(last_seen_at=None)
    device.refresh_from_db()
    report = {"battery": {"percent": 64, "charging": False}, "network": {"type": "cellular"},
              "location": {"lat": 23.81, "lon": 90.41, "accuracy": 9}}  # fmt: skip
    handle_device(device, check_in(answer["relay"], report))
    device.refresh_from_db()
    assert device.battery_percent == 64 and device.network == "cellular"
    assert float(device.latitude) == 23.81 and device.last_seen_at is not None


def test_forged_stale_and_too_frequent_check_ins_change_nothing(client, parent, sara):
    device, answer = paired_phone(client, parent, sara)
    login = answer["relay"]
    Device.objects.filter(pk=device.pk).update(last_seen_at=None, battery_percent=None)
    device.refresh_from_db()
    forged = {**login, "key": "00" * 32}
    handle_device(device, check_in(forged, {"battery": {"percent": 1}}))
    old = time.time() - 3 * 3600  # sat in the relay while it was down
    handle_device(device, check_in(login, {"battery": {"percent": 2}}, at=old))
    tampered = check_in(login, {"battery": {"percent": 3}})
    tampered["body"] = tampered["body"].replace('"percent": 3', '"percent": 99')
    handle_device(device, tampered)
    device.refresh_from_db()
    assert device.battery_percent is None and device.last_seen_at is None
    handle_device(device, check_in(login, {"battery": {"percent": 50}}))
    handle_device(device, check_in(login, {"battery": {"percent": 51}}))  # 30 s floor
    device.refresh_from_db()
    assert device.battery_percent == 50


def test_pairing_again_cuts_off_the_old_phone(client, parent, sara, fake_relay):
    device, first = paired_phone(client, parent, sara)
    client.force_login(parent)
    client.post(f"/devices/{device.pk}/pair/", {"action": "new_code"})
    page = client.get(f"/devices/{device.pk}/pair/").content.decode()
    client.logout()
    second = api_pair(client, test_devices.code_on(page)).json()
    device.refresh_from_db()
    Device.objects.filter(pk=device.pk).update(last_seen_at=None, battery_percent=None)
    device.refresh_from_db()
    handle_device(device, check_in(first["relay"], {"battery": {"percent": 5}}))
    device.refresh_from_db()
    assert device.battery_percent is None  # the old key no longer verifies
    assert second["relay"]["key"] != first["relay"]["key"]


def test_removing_a_phone_removes_its_relay_login(client, parent, sara, fake_relay):
    device, answer = paired_phone(client, parent, sara)
    fake_relay.rpush(answer["relay"]["inbox"], "waiting")
    device.delete()
    assert f"sm-d-{device.pk}" not in fake_relay.users
    assert answer["relay"]["inbox"] not in fake_relay.lists


def test_the_bridge_receives_phone_and_server_inboxes_in_one_call(client, parent, sara, fake_relay):
    device, answer = paired_phone(client, parent, sara)
    message = check_in(answer["relay"], {"battery": {"percent": 70}})
    fake_relay.rpush(answer["relay"]["inbox"], json.dumps(message))
    assert relay.receive([], 1, [device.pk]) == ("device", str(device.pk), message)
