"""Two-factor sign-in: authenticator app (TOTP) plus single-use recovery codes."""

import hashlib

import segno
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import totp
from .models import RecoveryCode, TOTPDevice

PENDING_KEY = "2fa_pending"
PENDING_SECONDS = 300


def device_for(user) -> TOTPDevice | None:
    return TOTPDevice.objects.filter(user=user).first()


def is_enabled(user) -> bool:
    device = device_for(user)
    return bool(device and device.is_active)


def is_required(user) -> bool:
    """Admins must use 2FA when REQUIRE_2FA_FOR_ADMINS is on (the default): this dashboard
    can deploy to every server."""
    if not settings.REQUIRE_2FA_FOR_ADMINS or not user.is_authenticated:
        return False
    from .accounts import role_of

    return role_of(user) == "Admin"


def start_setup(user, *, regenerate: bool = False) -> TOTPDevice:
    device = device_for(user)
    if device is None:
        return TOTPDevice.objects.create(user=user, secret=totp.generate_secret())
    if regenerate or not device.is_active:
        device.secret, device.confirmed_at, device.last_used_step = totp.generate_secret(), None, 0
        device.save()
    return device


def qr_svg(user, device: TOTPDevice) -> str:
    uri = totp.provisioning_uri(device.secret, user.get_username(), settings.SITE_NAME)
    return segno.make(uri, error="m").svg_inline(scale=4, border=2, dark="#1d1d1f", light="#ffffff")


def _hash(code: str) -> str:
    return hashlib.sha256(RecoveryCode.normalise(code).encode()).hexdigest()


@transaction.atomic
def issue_recovery_codes(user) -> list[str]:
    codes = totp.new_recovery_codes()
    RecoveryCode.objects.filter(user=user).delete()
    RecoveryCode.objects.bulk_create(RecoveryCode(user=user, code_hash=_hash(c)) for c in codes)
    return codes


@transaction.atomic
def confirm(device: TOTPDevice, code: str) -> list[str] | None:
    """Activate the device with a first valid code. Returns fresh recovery codes."""
    step = totp.verify(device.secret, code, last_step=device.last_used_step)
    if step is None:
        return None
    device.confirmed_at, device.last_used_step = timezone.now(), step
    device.save(update_fields=["confirmed_at", "last_used_step"])
    return issue_recovery_codes(device.user)


@transaction.atomic
def check(user, code: str) -> str | None:
    """'totp' or 'recovery' if `code` is a valid second factor (consuming it), else None."""
    device = (
        TOTPDevice.objects.select_for_update().filter(user=user, confirmed_at__isnull=False).first()
    )
    if device is None:
        return None
    step = totp.verify(device.secret, code, last_step=device.last_used_step)
    if step is not None:
        device.last_used_step = step
        device.save(update_fields=["last_used_step"])
        return "totp"
    recovery = (
        RecoveryCode.objects.select_for_update()
        .filter(user=user, code_hash=_hash(code), used_at__isnull=True)
        .first()
    )
    if recovery is not None:
        recovery.used_at = timezone.now()
        recovery.save(update_fields=["used_at"])
        return "recovery"
    return None


def remaining_recovery_codes(user) -> int:
    return RecoveryCode.objects.filter(user=user, used_at__isnull=True).count()


@transaction.atomic
def disable(user) -> None:
    TOTPDevice.objects.filter(user=user).delete()
    RecoveryCode.objects.filter(user=user).delete()
