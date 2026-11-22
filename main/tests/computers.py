"""Plays the NexGuard computer agent in tests: it pairs and checks in through the relay
only, never over HTTP to the dashboard, exactly as the agent does
(device-agent/internal/api/api.go)."""

import base64
import gzip
import json
import secrets
import time
import urllib.parse
from datetime import timedelta

from django.utils import timezone

from apps.agent_api import relay
from apps.agent_api.bridge import handle_device, handle_pairing
from apps.devices.models import Device

from .phones import answer, sign

INFO = {"model": "Dell XPS 13", "os_version": "Windows 11 (10.0.26100)",
        "app_version": "nexguard-agent 1.0.0"}  # fmt: skip


def ask(code: str, info: dict, *, preview: bool = False, secret: str | None = None,
        at: float | None = None) -> dict | None:  # fmt: skip
    """One pairing request through the shared pairing inbox, answered by the bridge."""
    nonce = secrets.token_hex(16)
    body = {"at": int(at if at is not None else time.time()), "nonce": nonce, "code": code,
            "info": info}  # fmt: skip
    if preview:
        body["preview"] = True
    else:
        body["secret"] = secret or secrets.token_hex(32)
    handle_pairing({"t": "pair", "body": json.dumps(body)})
    return answer(relay.client(), nonce)


class Computer:
    def __init__(self, device: Device, state: dict, secret: str):
        self.device, self.state, self.key = device, state, bytes.fromhex(secret)
        self.login = state["relay"]

    @property
    def password(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(self.login["url"]).password or "")

    def can_connect(self) -> bool:
        """Would the relay still take this computer's login? (Removing or re-pairing the
        device replaces or deletes it.)"""
        user = urllib.parse.urlsplit(self.login["url"]).username
        return f">{self.password}" in relay.client().users.get(user, [])

    def message(self, report: dict, at: float | None = None) -> dict:
        body = json.dumps({"at": int(at if at is not None else time.time()), "report": report})
        return {"t": "ci", "body": body, "sig": sign(self.key, body)}

    def checkin(self, report: dict | None = None, at: float | None = None) -> Device:
        """One check-in, as if the last one was a while ago (the relay keeps at most one per
        30 s). Returns the device as stored after it."""
        floor = timezone.now() - timedelta(seconds=31)
        Device.objects.filter(pk=self.device.pk, last_seen_at__gt=floor).update(last_seen_at=floor)
        device = Device.objects.select_related("account", "person", "created_by").get(
            pk=self.device.pk
        )
        handle_device(device, self.message(report or {}, at))
        device.refresh_from_db()
        return device

    def send(self, report: dict | None = None) -> dict:
        """Check in, then read the settings the dashboard left, as the agent does next:
        {"checkin_seconds", "controls", "device", "digest"} ({} when there are none)."""
        self.checkin(report)
        return self.settings() or {}

    def _read(self, key: str) -> str | None:
        raw = relay.client().strings.get(key)
        if raw is None:
            return None
        sealed = json.loads(raw)
        assert sealed["sig"] == sign(self.key, sealed["body"]), "not signed with the device key"
        return sealed["body"]

    def settings(self) -> dict | None:
        """What the dashboard left for the agent to GET (verified like the agent does), with
        "digest", the value the agent echoes back as `config`."""
        body = self._read(self.login["config"])
        if body is None:
            return None
        return {**json.loads(body), "digest": relay.config_digest(body)}

    def controls(self) -> dict | None:
        return (self.settings() or {}).get("controls")

    def blocklist(self) -> tuple[str, list[str]] | None:
        body = self._read(self.login["blocklist"])
        if body is None:
            return None
        data = json.loads(body)
        text = gzip.decompress(base64.b64decode(data["gz"])).decode()
        return data["version"], [line for line in text.split("\n") if line]


def pair_computer(device: Device, code: str, **info) -> Computer:
    """Pair with the code from the connect link, as the agent does. Asserts it worked."""
    secret = secrets.token_hex(32)
    state = ask(code, {"platform": device.platform, **INFO, **info}, secret=secret)
    assert state and state["state"] == "paired", state
    device = Device.objects.get(pk=device.pk)
    assert state["device"]["id"] == str(device.pk)
    return Computer(device, state, secret)
