"""Adding, pairing and hearing from devices. Views and the device API call these."""

import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.tenants.models import DESKTOP_APP_CLIENT

from . import pairing
from .models import (
    AGENT_APP,
    COMPUTERS,
    Device,
    DeviceLocation,
    DeviceSample,
    LocationSource,
    Platform,
)

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
    if (
        device.platform in COMPUTERS
        and device.account.computers_need_app
        and info.get("client") != DESKTOP_APP_CLIENT
    ):
        # Checked on previews too, so a terminal install stops before it asks anything.
        raise PairingError(f"{device.account.name} connects computers with the NexGuard app. "
                           "Install it, open it and enter this code there.", 403)  # fmt: skip
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
    if isinstance(report.get("web_no_access"), list):
        device.web_no_access = sorted(
            {_text(b, 30) for b in report["web_no_access"][:20] if _text(b, 30)}
        )
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
    if device.platform in COMPUTERS:
        _apply_health(device, report)
    location = _location(report.get("location"), now)
    record_location = location is not None
    if device.platform in COMPUTERS:
        location, record_location = _computer_location(device, location, ip, now)
    if location is not None:
        device.latitude, device.longitude = location["latitude"], location["longitude"]
        device.location_accuracy_m, device.location_at = location["accuracy_m"], location["at"]
    device.save()
    _record_sample(device, now)
    if "activity" in report:
        from . import activity

        activity.apply(device, report["activity"])
    if location is not None and record_location:
        DeviceLocation.objects.create(
            device=device,
            recorded_at=location["at"],
            latitude=location["latitude"],
            longitude=location["longitude"],
            accuracy_m=location["accuracy_m"],
            source=location["source"],
        )


# A town-level fix from the internet address is the same all day: keep one history point
# an hour while it doesn't change, not one per check-in.
IP_HISTORY_EVERY = timedelta(hours=1)


def _computer_location(device: Device, location, ip, now):
    """(location, record it in history) for a computer. Only with Location switched on
    under Controls: the agent's own fix if it sent one, else the town of its address."""
    from . import activity, geoip

    controls = activity.controls_for(device) if device.controls_allowed else None
    if controls is None or not controls.location:
        return None, False  # never stored unless switched on, whatever was sent
    if location is not None:
        return location, True
    fix = geoip.locate(ip)
    if fix is None:
        return None, False
    location = _location({**fix, "at": now.isoformat(), "source": "ip"}, now)
    same_place = (
        location is not None
        and device.latitude == location["latitude"]
        and device.longitude == location["longitude"]
    )
    recent = device.location_at is not None and now - device.location_at < IP_HISTORY_EVERY
    if same_place and recent:
        return None, False  # nothing new: keep the time of the last real point
    return location, True


def _apply_health(device: Device, report: dict) -> None:
    """CPU, memory and GPU use. Each is optional, and a check-in without one clears it:
    showing a stale "CPU 95%" from yesterday would be worse than "not reported"."""
    cpu = report.get("cpu") if isinstance(report.get("cpu"), dict) else {}
    gpu = report.get("gpu") if isinstance(report.get("gpu"), dict) else {}
    memory = report.get("memory") if isinstance(report.get("memory"), dict) else {}
    device.cpu_percent = _percent(cpu.get("percent"))
    device.gpu_percent = _percent(gpu.get("percent"))
    total = _int(memory.get("total"), 1, 10**14)
    used = _int(memory.get("used"), 0, 10**14)
    if total is not None and used is not None and used <= total:
        device.memory_total, device.memory_used = total, used
    else:
        device.memory_total = device.memory_used = None


def _percent(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 1) if 0 <= value <= 100 else None


def _record_sample(device: Device, now: datetime) -> None:
    sample = DeviceSample(
        device=device,
        recorded_at=now,
        cpu_percent=device.cpu_percent,
        memory_percent=device.memory_used_percent,
        gpu_percent=device.gpu_percent,
        battery_percent=device.battery_percent,
        disk_percent=device.storage_used_percent,
    )
    if any(getattr(sample, field) is not None for field in DeviceSample.METRICS):
        sample.save()


def _apply_info(device: Device, info: dict) -> None:
    for field, length in (("os_version", 100), ("model", 100), ("hostname", 100),
                          ("app_version", 30)):  # fmt: skip
        if field in info:
            setattr(device, field, _text(info.get(field), length))
    if "agent_mode" in info:
        mode = info.get("agent_mode")
        device.agent_mode = mode if mode in ("user", "system") else ""
    elif "app_version" in info and not device.app_version.startswith(AGENT_APP):
        device.agent_mode = ""  # paired again with the script


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


CHART_RANGES = {"6h": timedelta(hours=6), "24h": timedelta(hours=24),
                "7d": timedelta(days=7), "30d": timedelta(days=30)}  # fmt: skip
DEFAULT_CHART_RANGE = "24h"
CHART_POINTS = 120
CHART_SERIES = {"cpu": "cpu_percent", "memory": "memory_percent", "gpu": "gpu_percent",
                "battery": "battery_percent", "disk": "disk_percent"}  # fmt: skip


def chart_series(device: Device, range_key: str) -> dict:
    """Bucket-averaged health history for the range, at most CHART_POINTS per series, in the
    shape the `sparkline` tag draws: [[epoch, value], ...]. A series nothing reported is
    empty (the page hides it), and gaps (missing values) are skipped, not drawn as zero."""
    span = CHART_RANGES.get(range_key, CHART_RANGES[DEFAULT_CHART_RANGE])
    end = timezone.now()
    start = end - span
    bucket = span.total_seconds() / CHART_POINTS
    rows = (
        device.samples.filter(recorded_at__gte=start)
        .order_by("recorded_at")
        .values_list("recorded_at", *CHART_SERIES.values())
    )
    sums: dict[str, dict[int, list]] = {name: {} for name in CHART_SERIES}
    first = None
    for ts, *values in rows:
        first = first or ts
        idx = min(int((ts - start).total_seconds() // bucket), CHART_POINTS - 1)
        for name, value in zip(CHART_SERIES, values, strict=True):
            if value is not None:
                acc = sums[name].setdefault(idx, [ts, 0.0, 0])
                acc[0], acc[1], acc[2] = ts, acc[1] + value, acc[2] + 1
    series = {
        name: [
            [int(ts.timestamp()), round(total / n, 1)] for _, (ts, total, n) in sorted(b.items())
        ]
        for name, b in sums.items()
    }
    return {
        "start": int(start.timestamp()),
        "end": int(end.timestamp()),
        # Recording began partway through: say so, or a mostly empty chart looks broken.
        "data_from": first if first and first - start > span / 20 else None,
        **series,
    }


def prune_samples(batch_size: int = 5000) -> int:
    cutoff = timezone.now() - timedelta(days=settings.DEVICE_METRICS_DAYS)
    total = 0
    while ids := list(
        DeviceSample.objects.filter(recorded_at__lt=cutoff).values_list("pk", flat=True)[
            :batch_size
        ]
    ):
        total += DeviceSample.objects.filter(pk__in=ids).delete()[0]
    return total


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
