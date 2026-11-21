"""Plays the NexGuard phone app in tests: it pairs and checks in through the relay only,
never over HTTP to the dashboard, exactly as the app does (phone-app/src/relay.ts)."""

import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta

from django.utils import timezone

from apps.agent_api import relay
from apps.agent_api.bridge import handle_device, handle_pairing
from apps.devices.models import Device

INFO = {"platform": "android", "model": "Pixel 9", "os_version": "Android 16"}


def sign(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode(), hashlib.sha256).hexdigest()


def answer(fake, nonce: str) -> dict | None:
    raw = fake.strings.get(relay.pair_answer_key(nonce))
    return json.loads(raw) if raw else None


def pair_request(code: str, secret: str, nonce: str, info: dict, at: float | None = None) -> dict:
    """What the app RPUSHes to the shared pairing inbox: the code as typed."""
    body = json.dumps({"at": int(at if at is not None else time.time()), "nonce": nonce,
                       "code": code, "secret": secret, "info": info})  # fmt: skip
    return {"t": "pair", "body": body}


class Phone:
    def __init__(self, device: Device, state: dict, secret: str):
        self.device, self.state, self.key = device, state, bytes.fromhex(secret)

    @property
    def login(self) -> dict:
        return {**self.state["relay"], "key": self.key.hex()}

    def message(self, report: dict, at: float | None = None) -> dict:
        body = json.dumps({"at": int(at if at is not None else time.time()), "report": report})
        return {"t": "ci", "body": body, "sig": sign(self.key, body)}

    def checkin(self, report: dict, at: float | None = None) -> Device:
        """One check-in, as if the last one was a while ago (the relay keeps at most one per
        30 s, however fast a phone sends). Returns the device as stored after it."""
        floor = timezone.now() - timedelta(seconds=31)
        Device.objects.filter(pk=self.device.pk, last_seen_at__gt=floor).update(last_seen_at=floor)
        device = Device.objects.select_related("account", "person").get(pk=self.device.pk)
        handle_device(device, self.message(report, at))
        device.refresh_from_db()
        return device


def pair_phone(device: Device, code: str, **info) -> Phone:
    """Pair with the code shown on the dashboard, typed into the app. Asserts it worked."""
    secret, nonce = secrets.token_hex(32), secrets.token_hex(12)
    handle_pairing(pair_request(code, secret, nonce, {**INFO, **info}))
    state = answer(relay.client(), nonce)
    assert state and state["state"] == "paired", state
    device = Device.objects.get(pk=device.pk)
    assert state["device"]["id"] == str(device.pk)
    return Phone(device, state, secret)
