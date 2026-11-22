"""Alert rules for devices (computers and phones).

- Not reporting: opened by the sweep once a paired device has been silent for
  DEVICE_OFFLINE_ALERT_HOURS; resolved by its next check-in.
- Battery low: opened at check-in when the battery is at LOW_BATTERY % or less and not
  charging (critical at 5 %); resolved once it charges or climbs 5 points above the mark.
- SOS, place and tamper alerts are opened by their own modules through `open_alert`.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.devices.models import LOW_BATTERY, Device

from .models import Alert, AlertKind, Severity

logger = logging.getLogger(__name__)

BATTERY_CRITICAL = 5


def _open_alerts(device) -> dict:
    return {(a.kind, a.subject): a for a in Alert.objects.filter(device=device, resolved_at=None)}


def open_alert(device, kind, message, severity, subject="", value=None, open_alerts=None):
    """Open (or update) one alert on a device. Returns the alert."""
    open_alerts = _open_alerts(device) if open_alerts is None else open_alerts
    existing = open_alerts.get((kind, subject))
    if existing is not None:
        if (existing.value, existing.severity, existing.message) != (value, severity, message):
            existing.value, existing.severity, existing.message = value, severity, message[:300]
            existing.save(update_fields=["value", "severity", "message"])
        return existing
    try:
        with transaction.atomic():
            alert = Alert.objects.create(
                account_id=device.account_id, device=device, kind=kind, subject=subject,
                severity=severity, message=message[:300], value=value,
            )  # fmt: skip
    except IntegrityError:
        logger.info("Alert %s/%s on %s already open", kind, subject, device)
        return Alert.objects.filter(device=device, kind=kind, subject=subject,
                                    resolved_at=None).first()  # fmt: skip
    open_alerts[(kind, subject)] = alert
    return alert


def resolve_alert(device, kind, subject="", open_alerts=None) -> bool:
    open_alerts = _open_alerts(device) if open_alerts is None else open_alerts
    alert = open_alerts.pop((kind, subject), None)
    if alert is None:
        return False
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["resolved_at"])
    return True


def who(device) -> str:
    """ "Sara's phone" or "Family PC (Sara)"."""
    person = getattr(device, "person", None)
    if person and person.name.lower() not in device.name.lower():
        return f"{device.name} ({person.name})"
    return device.name


def evaluate_checkin(device: Device, report: dict | None = None, was_mode: str = "") -> None:
    """Run after each check-in has been saved."""
    report = report or {}
    open_alerts = _open_alerts(device)
    resolve_alert(device, AlertKind.DEVICE_OFFLINE, open_alerts=open_alerts)
    _tamper(device, report, was_mode, open_alerts)
    pct = device.battery_percent
    if pct is not None and pct <= LOW_BATTERY and not device.charging:
        severity = Severity.CRITICAL if pct <= BATTERY_CRITICAL else Severity.WARNING
        open_alert(device, AlertKind.BATTERY, f"Battery at {pct}% on {who(device)}, not charging",
                   severity, value=pct, open_alerts=open_alerts)  # fmt: skip
    elif device.charging or pct is None or pct > LOW_BATTERY + 5:
        resolve_alert(device, AlertKind.BATTERY, open_alerts=open_alerts)


BYE = {
    "uninstall": "NexGuard was uninstalled from {who}",
    "unpaired": "{who} was unpaired on the device itself",
}
TAMPER_EVENTS = {
    "hosts_edited": "Someone changed the blocked-sites list on {who}; NexGuard put it back",
    # Android phones (the app's filter service notices, and tells at once)
    "admin_off": "NexGuard's removal protection was turned off on {who}: it can now be uninstalled",
    "vpn_off": "NexGuard's website filter was turned off on {who} (another VPN, or it was "
    "switched off): sites aren't blocked or recorded",
    "private_dns": "Private DNS was set on {who}, which gets around NexGuard's website filter",
    "usage_access_off": "Usage access was taken away from NexGuard on {who}: screen time "
    "isn't recorded",
    "notifications_off": "Notifications were turned off for NexGuard on {who}: it can't beep",
}


def _event(device, subject, message) -> None:
    Alert.objects.create(
        account_id=device.account_id, device=device, kind=AlertKind.TAMPER, is_event=True,
        subject=f"{subject}:{int(timezone.now().timestamp())}", severity=Severity.WARNING,
        message=message[:300], resolved_at=timezone.now(),
    )  # fmt: skip


def _tamper(device, report, was_mode, open_alerts) -> None:
    """Protection removed or worked around: told once, as it happens."""
    name = who(device)
    bye = report.get("bye")
    if bye in BYE:
        open_alert(device, AlertKind.TAMPER, BYE[bye].format(who=name), Severity.WARNING,
                   subject="bye", open_alerts=open_alerts)  # fmt: skip
        return
    resolve_alert(device, AlertKind.TAMPER, "bye", open_alerts=open_alerts)  # it's back
    for code in report.get("tamper") or []:
        if code in TAMPER_EVENTS:
            _event(device, code, TAMPER_EVENTS[code].format(who=name))
    if was_mode == "system" and device.agent_mode == "user":
        _event(device, "mode", f"NexGuard on {name} now runs without administrator rights, "
               "so it can't block websites")  # fmt: skip
    permissions = report.get("permissions")
    if isinstance(permissions, dict) and isinstance(permissions.get("location"), bool):
        if permissions["location"]:
            resolve_alert(device, AlertKind.TAMPER, "location", open_alerts=open_alerts)
        elif device.location_at is not None:  # it was sharing before
            open_alert(device, AlertKind.TAMPER, f"Location sharing was turned off on {name}",
                       Severity.WARNING, subject="location", open_alerts=open_alerts)  # fmt: skip


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} h" if hours >= 1 else f"{delta.total_seconds() // 60:.0f} min"
    return f"{hours / 24:.0f} days"


def sweep(now=None) -> int:
    """Open "not reporting" alerts for paired devices silent too long. Returns how many."""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.DEVICE_OFFLINE_ALERT_HOURS)
    silent = (
        Device.objects.exclude(token_hash="")
        .filter(last_seen_at__lt=cutoff)
        .exclude(
            pk__in=Alert.objects.filter(
                kind=AlertKind.DEVICE_OFFLINE, resolved_at=None, device__isnull=False
            ).values("device_id")
        )
        .select_related("person")
    )
    # An acknowledged SOS the phone never cancelled closes after an hour.
    Alert.objects.filter(kind=AlertKind.SOS, resolved_at=None,
                         acknowledged_at__lt=now - timedelta(hours=1)).update(resolved_at=now)  # fmt: skip
    opened = 0
    for device in silent:
        open_alert(device, AlertKind.DEVICE_OFFLINE,
                   f"{who(device)} hasn't reported for {_ago(now - device.last_seen_at)}",
                   Severity.WARNING)  # fmt: skip
        opened += 1
    return opened


def resolve_all(device: Device) -> None:
    Alert.objects.filter(device=device, resolved_at=None).update(resolved_at=timezone.now())
