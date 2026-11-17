"""Adding, pairing and hearing from devices. Views and the device API call these."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import pairing
from .models import Device, DeviceLocation, LocationSource, Platform

logger = logging.getLogger(__name__)


class PairingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def new_pairing_code(device: Device) -> str:
    """A fresh code for this device; any earlier code stops working. The device keeps
    checking in with its current token until a device pairs with the new code."""
    code, code_hash = pairing.new_code()
    device.pairing_code_hash = code_hash
    device.save(update_fields=["pairing_code_hash"])
    return code


def cancel_pairing_code(device: Device) -> None:
    device.pairing_code_hash = ""
    device.save(update_fields=["pairing_code_hash"])


def _device_for_code(code: str, info: dict, *, lock: bool) -> Device:
    """The device waiting for this code, checked against the platform asking for it."""
    normalized = pairing.normalize_code(code)
    if len(normalized) != pairing.CODE_LENGTH:
        raise PairingError("Enter the 8-character code from the parent's dashboard.")
    devices = Device.objects.select_related("account", "person")
    if lock:
        devices = devices.select_for_update()
    device = devices.filter(pairing_code_hash=pairing.digest(normalized)).first()
    if device is None:
        raise PairingError("That code isn't valid. Ask for a new one on the dashboard.", 404)
    platform = str(info.get("platform", "")).lower()
    if platform and platform != device.platform:
        expected = Platform(device.platform).label
        raise PairingError(f"This code is for {_article(expected)} {expected}. Use it on that "
                           "device, or add this one separately.", 409)  # fmt: skip
    return device


def preview(code: str, info: dict) -> Device:
    """Who would see this device, before anything is stored: the device asks its user
    first and pairs only on a yes. The code stays valid either way."""
    return _device_for_code(code, info, lock=False)


@transaction.atomic
def pair(code: str, info: dict, ip: str | None) -> tuple[Device, str]:
    """Exchange a pairing code for a device token. The code is used up; a device already
    paired under this entry (reset, or a replacement phone) loses its old token."""
    device = _device_for_code(code, info, lock=True)
    raw, token_hash = pairing.new_token()
    now = timezone.now()
    device.token_hash = token_hash
    device.pairing_code_hash = ""
    device.paired_at = device.last_seen_at = now
    device.last_ip = ip
    _apply_info(device, info)
    device.save()
    from apps.audit.services import record

    record(None, "device.pair", target=device.name, account=device.account, from_ip=ip,
           platform=device.platform, model=device.model)  # fmt: skip
    return device, raw


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def authenticate(raw_token: str) -> Device | None:
    if not raw_token.startswith(pairing.TOKEN_PREFIX):
        return None
    return (
        Device.objects.select_related("account", "person")
        .filter(token_hash=pairing.digest(raw_token))
        .first()
    )


def checkin(device: Device, report: dict, ip: str | None) -> None:
    now = timezone.now()
    device.last_seen_at = now
    device.last_ip = ip
    _apply_info(device, report)
    battery = report.get("battery") or {}
    if isinstance(battery, dict):
        device.battery_percent = _int(battery.get("percent"), 0, 100)
        charging = battery.get("charging")
        device.charging = charging if isinstance(charging, bool) else None
    storage = report.get("storage") or {}
    if isinstance(storage, dict):
        total = _int(storage.get("total"), 1, 10**15)
        free = _int(storage.get("free"), 0, 10**15)
        if total is not None and free is not None and free <= total:
            device.storage_total, device.storage_free = total, free
    network = report.get("network") or {}
    if isinstance(network, dict):
        kind = str(network.get("type") or "").lower()
        device.network = kind if kind in {"wifi", "cellular", "ethernet", "none"} else ""
        device.network_name = _text(network.get("name"), 100)
    location = _location(report.get("location"), now)
    if location is not None:
        device.latitude, device.longitude = location["latitude"], location["longitude"]
        device.location_accuracy_m, device.location_at = location["accuracy_m"], location["at"]
    device.save()
    if location is not None:
        DeviceLocation.objects.create(
            device=device,
            recorded_at=location["at"],
            latitude=location["latitude"],
            longitude=location["longitude"],
            accuracy_m=location["accuracy_m"],
            source=location["source"],
        )


def _apply_info(device: Device, info: dict) -> None:
    for field, length in (("os_version", 100), ("model", 100), ("hostname", 100),
                          ("app_version", 30)):  # fmt: skip
        if field in info:
            setattr(device, field, _text(info.get(field), length))


def _text(value, length: int) -> str:
    return str(value).strip()[:length] if isinstance(value, str | int | float) else ""


def _int(value, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = int(value)
    return value if low <= value <= high else None


def _location(value, now: datetime) -> dict | None:
    """A sane location or None. Bad fields drop the location, never the check-in."""
    if not isinstance(value, dict):
        return None
    try:
        lat = Decimal(str(value["lat"])).quantize(Decimal("0.000001"))
        lon = Decimal(str(value["lon"])).quantize(Decimal("0.000001"))
    except KeyError, InvalidOperation, ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    at = parse_datetime(value["at"]) if isinstance(value.get("at"), str) else None
    if at is None or timezone.is_naive(at):
        at = now
    # A phone may send a fix it took while offline, but not one from the future.
    if at > now + timedelta(minutes=5) or at < now - timedelta(days=7):
        at = now
    source = str(value.get("source") or "gps").lower()
    return {
        "latitude": lat,
        "longitude": lon,
        "accuracy_m": _int(value.get("accuracy"), 0, 1_000_000),
        "at": at,
        "source": source if source in LocationSource.values else LocationSource.GPS,
    }


def prune_locations(batch_size: int = 5000) -> int:
    cutoff = timezone.now() - timedelta(days=settings.DEVICE_LOCATION_DAYS)
    total = 0
    while True:
        ids = list(
            DeviceLocation.objects.filter(recorded_at__lt=cutoff).values_list("pk", flat=True)[
                :batch_size
            ]
        )
        if not ids:
            return total
        total += DeviceLocation.objects.filter(pk__in=ids).delete()[0]
