"""1.3: two-factor sign-in."""

import time

import pytest
from django.urls import reverse

from apps.audit.models import AuditLog
from apps.users import totp, twofactor
from apps.users.models import RecoveryCode

pytestmark = pytest.mark.django_db

PASSWORD = "correct horse battery 9"


# RFC 6238 appendix B, SHA-1 column (8 digits), secret "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize(
    "at,code",
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],  # fmt: skip
)
def test_rfc6238_vectors(at, code):
    assert totp.totp(RFC_SECRET, at=at, digits=8) == code


def test_window_and_replay():
    secret = totp.generate_secret()
    now = 1_700_000_000
    code = totp.totp(secret, at=now)
    step = totp.verify(secret, code, at=now + 30)  # one step of drift is fine
    assert step == now // 30
    assert totp.verify(secret, code, at=now, last_step=step) is None  # replay refused
    assert totp.verify(secret, code, at=now + 90) is None  # too old
    assert totp.verify(secret, "12345", at=now) is None


def _user_with_2fa(django_user_model, roles, role="Admin"):
    from django.contrib.auth.models import Group

    user = django_user_model.objects.create_user("ada", password=PASSWORD)
    user.groups.add(Group.objects.get(name=role))
    device = twofactor.start_setup(user)
    codes = twofactor.confirm(device, totp.totp(device.secret, at=time.time() - 30))
    return user, device, codes


def _login(client, password=PASSWORD):
    return client.post(reverse("login"), {"username": "ada", "password": password})


def test_password_alone_does_not_sign_in(client, django_user_model, roles):
    _user_with_2fa(django_user_model, roles)
    resp = _login(client)
    assert resp.url == reverse("login_verify")
    assert "_auth_user_id" not in client.session
    assert client.get(reverse("fleet:dashboard")).status_code == 302  # still signed out


def test_code_completes_sign_in(client, django_user_model, roles):
    user, device, _ = _user_with_2fa(django_user_model, roles)
    _login(client)
    resp = client.post(reverse("login_verify"), {"code": totp.totp(device.secret)})
    assert resp.status_code == 302 and client.session["_auth_user_id"] == str(user.pk)
    assert AuditLog.objects.filter(action="auth.2fa_verified").exists()


def test_wrong_code_and_throttle(client, django_user_model, roles):
    _user_with_2fa(django_user_model, roles)
    _login(client)
    for _ in range(5):
        assert (
            "That code" in client.post(reverse("login_verify"), {"code": "000000"}).content.decode()
        )
    assert (
        "Too many attempts"
        in client.post(reverse("login_verify"), {"code": "000000"}).content.decode()
    )
    assert "_auth_user_id" not in client.session


def test_pending_step_expires(client, django_user_model, roles):
    _, device, _ = _user_with_2fa(django_user_model, roles)
    _login(client)
    session = client.session
    session[twofactor.PENDING_KEY]["at"] -= twofactor.PENDING_SECONDS + 1
    session.save()
    resp = client.post(reverse("login_verify"), {"code": totp.totp(device.secret)})
    assert resp.url == reverse("login") and "_auth_user_id" not in client.session


def test_recovery_codes_are_single_use(client, django_user_model, roles):
    user, _, codes = _user_with_2fa(django_user_model, roles)
    assert (
        len(codes) == 10 and not RecoveryCode.objects.filter(code_hash=codes[0]).exists()
    )  # hashed
    assert twofactor.check(user, codes[0].upper()) == "recovery"
    assert twofactor.check(user, codes[0]) is None
    assert twofactor.remaining_recovery_codes(user) == 9


def test_admins_are_forced_into_setup(client, admin_user, viewer, settings):
    settings.REQUIRE_2FA_FOR_ADMINS = True
    client.force_login(admin_user)
    assert client.get(reverse("fleet:dashboard")).url == reverse("account_2fa")
    assert client.get(reverse("account_2fa")).status_code == 200
    client.force_login(viewer)  # only Admins are forced
    assert client.get(reverse("fleet:dashboard")).status_code == 200


def test_setup_then_disable_needs_password_and_code(client, django_user_model, roles):
    from django.contrib.auth.models import Group

    user = django_user_model.objects.create_user("bo", password=PASSWORD)
    user.groups.add(Group.objects.get(name="Viewer"))
    client.force_login(user)
    assert "Set up your authenticator" in client.get(reverse("account_2fa")).content.decode()
    device = twofactor.device_for(user)
    page = client.post(
        reverse("account_2fa"), {"action": "confirm", "code": totp.totp(device.secret)}
    )
    assert "recovery codes" in page.content.decode() and twofactor.is_enabled(user)
    resp = client.post(
        reverse("account_2fa"), {"action": "disable", "password": "wrong", "code": "1"}
    )
    assert "Enter your password" in resp.content.decode() and twofactor.is_enabled(user)
    device.refresh_from_db()
    code = totp.totp(device.secret, at=time.time() + 30)  # next step: the last one was used
    client.post(reverse("account_2fa"), {"action": "disable", "password": PASSWORD, "code": code})
    assert not twofactor.is_enabled(user)


def test_admin_can_reset_someone_elses_2fa(client, admin_user, django_user_model, roles):
    user, _, _ = _user_with_2fa(django_user_model, roles, role="Viewer")
    client.force_login(admin_user)
    client.post(reverse("users:detail", args=[user.pk]), {"action": "reset_2fa"})
    assert not twofactor.is_enabled(user)
    assert AuditLog.objects.filter(action="user.2fa_reset").exists()


def test_break_glass_command(django_user_model, roles):
    from django.core.management import call_command

    user, _, _ = _user_with_2fa(django_user_model, roles)
    call_command("reset_2fa", "ADA")
    assert not twofactor.is_enabled(user)
