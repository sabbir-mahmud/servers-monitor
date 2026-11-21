"""Phones talk only to the Redis relay (Upstash REST), never to the dashboard: the person
types the dashboard's 6-digit code into the app, which sends it through the app's shared,
send-only pairing login; the answer carries the phone's own check-in login. Then it checks
in with a key it made itself (apps/agent_api/relay.py, bridge.py)."""

import json
import secrets
import time
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.agent_api import bridge, relay
from apps.agent_api.bridge import handle_device, handle_pairing
from apps.devices.models import Device

from . import test_devices
from .phones import Phone, answer, pair_phone, pair_request, sign
from .test_devices import add_device, api_pair, code_on

family, parent, sara = test_devices.family, test_devices.parent, test_devices.sara
solo_dev = test_devices.solo_dev

pytestmark = pytest.mark.django_db


def paired_phone(client, parent, sara, name="Sara's phone") -> tuple[Device, Phone]:
    client.force_login(parent)
    device, code = add_device(client, sara, name=name)
    client.logout()
    phone = pair_phone(device, code)
    return phone.device, phone


def check_in(login: dict, report: dict, at: float | None = None) -> dict:
    """What the phone app RPUSHes: the body text, signed with its key."""
    body = json.dumps({"at": int(at if at is not None else time.time()), "report": report})
    return {"t": "ci", "body": body, "sig": sign(bytes.fromhex(login["key"]), body)}


def try_code(fake, code: str, **info) -> dict:
    nonce = secrets.token_hex(12)
    handle_pairing(
        pair_request(code, secrets.token_hex(32), nonce, {"platform": "android", **info})
    )
    return answer(fake, nonce)


# --------------------------------------------------------------------------- pairing


def test_the_app_login_can_only_send_pairing_requests_and_read_answers(fake_relay, settings):
    login = relay.provision_app_login()
    assert login == {"url": "https://relay.test", "token": "rest_sm-app", "prefix": "sm"}
    rules = fake_relay.users["sm-app"]
    assert {"~sm:pair:in", "~sm:pair:out:*", "-@all"} <= set(rules)
    assert {r for r in rules if r.startswith("+")} == {"+rpush", "+get", "+ping"}


def test_phone_app_login_command_prints_the_env_lines(fake_relay, capsys):
    call_command("phone_app_login")
    out = capsys.readouterr().out
    assert "EXPO_PUBLIC_REDIS_URL=https://relay.test" in out
    assert "EXPO_PUBLIC_REDIS_TOKEN=rest_sm-app" in out and "EXPO_PUBLIC_REDIS_PREFIX=sm" in out


def test_a_phone_gets_a_unique_six_digit_code_that_expires(client, parent, sara):
    client.force_login(parent)
    device, code, page = add_device(client, sara, with_page=True)
    assert len(code) == 7 and code.replace(" ", "").isdigit()  # "482 913"
    assert (
        "Open NexGuard and enter this code" in page
        and "<svg" not in page.split("pair-code")[1][:400]
    )
    device.refresh_from_db()
    left = device.pairing_code_expires_at - timezone.now()
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)
    other, other_code = add_device(client, sara, name="Sara's tablet")
    assert other_code != code


def test_pairing_through_the_relay(client, parent, sara, fake_relay):
    device, phone = paired_phone(client, parent, sara)
    state = phone.state
    assert state["relay"] == {"url": "https://relay.test", "token": f"rest_sm-d-{device.pk}",
                              "inbox": f"sm:d:{device.pk}:in"}  # fmt: skip
    assert "key" not in state["relay"]  # the phone made its own; it never travels back
    assert state["name"] == "Sara" and state["device"]["person"] == "Sara"
    assert "The Rahmans will see" in state["disclosure"] and state["checkin_seconds"] == 900
    assert device.is_paired and device.pairing_code_hash == "" and device.model == "Pixel 9"
    assert device.relay_key_enc and phone.key.hex() not in device.relay_key_enc  # encrypted
    rules = fake_relay.users[f"sm-d-{device.pk}"]
    assert {r for r in rules if r.startswith("+")} == {"+rpush", "+ping"}  # push only
    from apps.audit.models import AuditLog

    assert AuditLog.objects.filter(action="device.pair", target=device.name).exists()


def test_connected_as_the_user_who_added_it_when_its_nobodys(client, solo_dev):
    client.force_login(solo_dev)
    resp = client.post("/devices/new/", {"platform": "android", "name": "My phone"})
    device = Device.objects.get(name="My phone")
    code = code_on(client.get(resp["Location"]).content.decode())
    assert pair_phone(device, code).state["name"] == solo_dev.display_name


def test_wrong_expired_used_or_other_platform_codes_do_not_pair(client, parent, sara, fake_relay):
    client.force_login(parent)
    device, code = add_device(client, sara)
    wrong = "000 000" if code != "000 000" else "111 111"
    assert "isn't valid" in try_code(fake_relay, wrong)["message"]
    assert "6-digit" in try_code(fake_relay, "12345")["message"]
    assert "Android" in try_code(fake_relay, code, platform="ios")["message"]
    Device.objects.filter(pk=device.pk).update(pairing_code_expires_at=timezone.now())
    assert "expired" in try_code(fake_relay, code)["message"]
    device.refresh_from_db()
    assert not device.is_paired
    client.post(f"/devices/{device.pk}/pair/", {"action": "new_code"})
    code = code_on(client.get(f"/devices/{device.pk}/pair/").content.decode())
    assert try_code(fake_relay, code)["state"] == "paired"
    assert "isn't valid" in try_code(fake_relay, code)["message"]  # single use


def test_guessing_codes_is_capped(client, parent, sara, fake_relay):
    client.force_login(parent)
    device, code = add_device(client, sara)
    guesses = [f"{n:06d}" for n in range(1000) if f"{n:06d}" != code.replace(" ", "")]
    for guess in guesses[: bridge.PAIR_FAILURE_LIMIT]:
        try_code(fake_relay, guess)
    assert "Too many wrong codes" in try_code(fake_relay, code)["message"]  # even the right one
    assert not Device.objects.get(pk=device.pk).is_paired


def test_stale_or_unanswerable_requests_are_dropped(client, parent, sara, fake_relay):
    client.force_login(parent)
    device, code = add_device(client, sara)
    old = pair_request(code, secrets.token_hex(32), "ab" * 12, {}, at=time.time() - 3600)
    handle_pairing(old)
    handle_pairing({"t": "pair", "body": json.dumps({"code": code, "nonce": "../x"})})
    assert not fake_relay.strings and not Device.objects.get(pk=device.pk).is_paired


def test_phones_cannot_pair_over_http(client, parent, sara):
    client.force_login(parent)
    device, code = add_device(client, sara)
    client.logout()
    for preview in (False, True):
        resp = api_pair(client, code, preview=preview)
        assert resp.status_code == 410 and "Update the app" in resp.json()["error"]
    device.refresh_from_db()
    assert not device.is_paired and device.pairing_code_hash


def test_without_upstash_the_page_says_phones_cant_pair(client, parent, sara, settings):
    settings.RELAY_REST_URL = ""  # REDIS_URL is relay.test: no REST API
    client.force_login(parent)
    resp = client.post("/devices/new/", {"platform": "android", "name": "P", "person": sara.pk})
    page = client.get(resp["Location"]).content.decode()
    assert "can't pair phones yet" in page and 'id="pair-code"' not in page


# --------------------------------------------------------------------------- check-ins


def test_a_signed_check_in_through_the_relay_updates_the_phone(client, parent, sara, fake_relay):
    device, phone = paired_phone(client, parent, sara)
    report = {"battery": {"percent": 64, "charging": False}, "network": {"type": "cellular"},
              "location": {"lat": 23.81, "lon": 90.41, "accuracy": 9}}  # fmt: skip
    device = phone.checkin(report)
    assert device.battery_percent == 64 and device.network == "cellular"
    assert float(device.latitude) == 23.81 and device.last_seen_at is not None


def test_forged_stale_and_too_frequent_check_ins_change_nothing(client, parent, sara):
    device, phone = paired_phone(client, parent, sara)
    login = phone.login
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


def test_phones_paired_before_still_check_in_with_the_old_key(client, parent, sara):
    """Paired over HTTP by an older app: no phone key, signs with the salt-derived key."""
    from apps.agent_api import signing

    device, phone = paired_phone(client, parent, sara)
    Device.objects.filter(pk=device.pk).update(relay_key_enc="", last_seen_at=None)
    device.refresh_from_db()
    legacy = {"key": signing.device_key(device).hex()}
    handle_device(device, check_in(phone.login, {"battery": {"percent": 7}}))  # new key: no
    handle_device(device, check_in(legacy, {"battery": {"percent": 8}}))
    device.refresh_from_db()
    assert device.battery_percent == 8


def test_pairing_again_cuts_off_the_old_phone(client, parent, sara, fake_relay):
    device, first = paired_phone(client, parent, sara)
    client.force_login(parent)
    client.post(f"/devices/{device.pk}/pair/", {"action": "new_code"})
    page = client.get(f"/devices/{device.pk}/pair/").content.decode()
    assert first.checkin({"battery": {"percent": 4}}).battery_percent == 4  # meanwhile: fine
    second = pair_phone(device, code_on(page))
    assert first.checkin({"battery": {"percent": 5}}).battery_percent == 4  # old key: refused
    assert second.checkin({"battery": {"percent": 6}}).battery_percent == 6
    assert second.key != first.key


def test_removing_a_phone_removes_its_relay_login(client, parent, sara, fake_relay):
    device, phone = paired_phone(client, parent, sara)
    fake_relay.rpush(phone.login["inbox"], "waiting")
    Device.objects.get(pk=device.pk).delete()
    assert f"sm-d-{device.pk}" not in fake_relay.users
    assert phone.login["inbox"] not in fake_relay.lists


def test_the_bridge_listens_for_pairing_only_while_a_phone_code_is_out(client, parent, sara,
                                                                     fake_relay):  # fmt: skip
    device, phone = paired_phone(client, parent, sara)
    message = phone.message({"battery": {"percent": 70}})
    fake_relay.rpush(phone.login["inbox"], json.dumps(message))
    assert relay.receive([], 1, [device.pk]) == ("device", str(device.pk), message)
    request = pair_request("123456", "00" * 32, "ab" * 12, {})
    fake_relay.rpush(relay.pair_inbox_key(), json.dumps(request))
    assert relay.receive([], 1, [], pairing=False) is None
    assert relay.receive([], 1, [], pairing=True) == ("pairing", "", request)
