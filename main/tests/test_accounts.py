"""Accounts: sign-in by username or email, invitations, sign-up with approval, password
reset (email and admin link), user management guards, and the security properties."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.users import accounts
from apps.users.models import Invitation

pytestmark = pytest.mark.django_db
User = get_user_model()
GOOD_PASSWORD = "correct-horse-battery-9"


@pytest.fixture(autouse=True)
def sync_mail(monkeypatch):
    """Send mail inline so the test can read mail.outbox right away."""
    monkeypatch.setattr(accounts, "send_async", accounts._send)


@pytest.fixture
def email_on(settings):
    settings.EMAIL_ENABLED = True


def make_user(username, role="Viewer", password=GOOD_PASSWORD, **kw):
    user = User.objects.create_user(username, password=password, **kw)
    if role:
        user.groups.add(Group.objects.get(name=role))
    return user


def login(client, username, password=GOOD_PASSWORD):
    return client.post(reverse("login"), {"username": username, "password": password})


# ---------------------------------------------------------------- sign in


def test_sign_in_with_username_or_email_case_insensitive(client, roles):
    make_user("sabbir", email="Sabbir@Example.com")
    assert login(client, "SABBIR")["Location"] == reverse("fleet:dashboard")
    client.logout()
    assert login(client, "sabbir@example.com").status_code == 302


def test_pending_account_explained_only_with_right_password(client, roles):
    make_user("newbie", role=None, is_active=False, pending_approval=True)
    assert b"waiting for an administrator" in login(client, "newbie").content
    assert b"waiting for an administrator" not in login(client, "newbie", "wrong-pass-123").content


def test_emails_are_unique_case_insensitively(roles):
    make_user("a", email="ops@example.com")
    with pytest.raises(IntegrityError):
        make_user("b", email="OPS@example.com")


# ---------------------------------------------------------------- forgot password


def _normalised(response, email) -> list[str]:
    """Page lines with the per-request noise removed: the submitted address, CSRF tokens
    and CSP nonces. What's left must not depend on whether the account exists."""
    import re

    text = response.content.decode().replace(email, "EMAIL")
    text = re.sub(
        r'(csrfmiddlewaretoken" value=|X-CSRFToken": |nonce=)"?[^"\s>]+', r"\1TOKEN", text
    )
    return text.splitlines()


def test_forgot_password_sends_link_and_never_reveals_accounts(client, roles, email_on, mailoutbox):
    make_user("sabbir", email="sabbir@example.com")
    known = client.post(reverse("password_forgot"), {"email": "sabbir@example.com"})
    unknown = client.post(reverse("password_forgot"), {"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert _normalised(known, "sabbir@example.com") == _normalised(unknown, "nobody@example.com")
    (message,) = mailoutbox
    assert message.to == ["sabbir@example.com"] and "/password/reset/" in message.body
    assert AuditLog.objects.filter(action="auth.password_reset_requested").count() == 2


def test_forgot_password_skips_inactive_accounts_and_rate_limits(
    client, roles, email_on, mailoutbox
):
    make_user("gone", email="gone@example.com", is_active=False)
    client.post(reverse("password_forgot"), {"email": "gone@example.com"})
    assert mailoutbox == []
    for _ in range(3):
        client.post(reverse("password_forgot"), {"email": "x@example.com"})
    assert (
        b"Too many reset requests"
        in client.post(reverse("password_forgot"), {"email": "x@example.com"}).content
    )


def test_without_email_the_page_points_to_an_admin(client, roles, settings):
    settings.EMAIL_ENABLED = False
    page = client.get(reverse("password_forgot")).content.decode()
    assert "an administrator resets passwords" in page and 'name="email"' not in page


def _reset_via(client, link, password="brand-new-password-42"):
    path = link.split("testserver")[-1]
    landing = client.get(path)
    assert landing.status_code == 302  # Django hides the token from the address bar
    return client.post(landing["Location"], {"new_password1": password, "new_password2": password})


def test_reset_link_works_once(client, rf, roles):
    user = make_user("sabbir", email="s@example.com")
    request = rf.get("/")
    link = accounts.reset_link(request, user)
    assert _reset_via(client, link)["Location"] == reverse("login")
    assert login(client, "sabbir", "brand-new-password-42").status_code == 302
    client.logout()
    reused = client.get(link.split("testserver")[-1], follow=True)
    assert (
        b"doesn&#x27;t work any more" in reused.content
        or b"doesn't work any more" in reused.content
    )
    assert AuditLog.objects.filter(action="auth.password_reset", target="sabbir").exists()


def test_reset_rejects_weak_password(client, rf, roles):
    user = make_user("sabbir")
    resp = _reset_via(client, accounts.reset_link(rf.get("/"), user), password="123456789")
    assert resp.status_code == 200
    user.refresh_from_db()
    assert user.check_password(GOOD_PASSWORD)


# ---------------------------------------------------------------- invitations


def test_invite_accept_creates_account_with_role(client, admin_user):
    client.force_login(admin_user)
    page = client.post(reverse("users:invite"), {"email": "dev@example.com", "role": "Operator"})
    link = page.content.decode().split('id="invite-link" class="text-break">')[1].split("<")[0]
    client.logout()
    path = link.split("testserver")[-1]
    assert b"dev@example.com" in client.get(path).content
    resp = client.post(path, {"username": "dev", "first_name": "Dev", "last_name": "One",
                              "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD})  # fmt: skip
    assert resp["Location"] == reverse("fleet:dashboard")
    user = User.objects.get(username="dev")
    assert user.email == "dev@example.com" and accounts.role_of(user) == "Operator"
    assert client.get(reverse("fleet:dashboard")).status_code == 200  # signed in
    client.logout()
    assert client.get(path).status_code == 404  # single use


def test_invitations_expire_and_can_be_revoked(client, admin_user):
    expired, raw1 = Invitation.issue("a@example.com", "Viewer", admin_user)
    Invitation.objects.filter(pk=expired.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert client.get(reverse("accept_invite", args=[raw1])).status_code == 404
    live, raw2 = Invitation.issue("b@example.com", "Viewer", admin_user)
    client.force_login(admin_user)
    client.post(reverse("users:invite_revoke", args=[live.pk]))
    client.logout()
    assert client.get(reverse("accept_invite", args=[raw2])).status_code == 404
    assert client.get(reverse("accept_invite", args=["not-a-real-token"])).status_code == 404


def test_reinviting_replaces_the_old_link(client, admin_user):
    client.force_login(admin_user)
    client.post(reverse("users:invite"), {"email": "dev@example.com", "role": "Viewer"})
    client.post(reverse("users:invite"), {"email": "DEV@example.com", "role": "Admin"})
    states = sorted(i.state for i in Invitation.objects.all())
    assert states == ["pending", "revoked"]


def test_cannot_invite_an_existing_address(client, admin_user):
    make_user("taken", email="taken@example.com")
    client.force_login(admin_user)
    resp = client.post(reverse("users:invite"), {"email": "Taken@example.com", "role": "Viewer"})
    assert b"already has an account" in resp.content and not Invitation.objects.exists()


def test_invitation_token_stored_hashed(admin_user):
    invite, raw = Invitation.issue("x@example.com", "Viewer", admin_user)
    assert raw not in invite.token_hash and Invitation.find(raw) == invite


def test_invite_email_sent_when_configured(client, admin_user, email_on, mailoutbox):
    client.force_login(admin_user)
    client.post(reverse("users:invite"), {"email": "dev@example.com", "role": "Viewer"})
    (message,) = mailoutbox
    assert message.to == ["dev@example.com"] and "/invite/" in message.body


# ---------------------------------------------------------------- sign-up


def test_signup_off_by_default(client):
    assert client.get(reverse("signup")).status_code == 404


def test_signup_needs_admin_approval(client, admin_user, settings):
    settings.ALLOW_SIGNUP = True
    resp = client.post(reverse("signup"), {
        "email": "new@example.com", "username": "newbie", "first_name": "New", "last_name": "",
        "password1": GOOD_PASSWORD, "password2": GOOD_PASSWORD,
    })  # fmt: skip
    assert b"waiting for an administrator" in resp.content
    user = User.objects.get(username="newbie")
    assert not user.is_active and user.pending_approval and accounts.role_of(user) is None
    assert login(client, "newbie").status_code == 200  # can't sign in yet
    client.force_login(admin_user)
    client.post(reverse("users:detail", args=[user.pk]), {"action": "approve", "role": "Operator"})
    client.logout()
    assert login(client, "newbie").status_code == 302
    user.refresh_from_db()
    assert accounts.role_of(user) == "Operator" and not user.pending_approval


# ---------------------------------------------------------------- management guards


def test_admin_cannot_lock_themselves_out(client, admin_user):
    client.force_login(admin_user)
    client.post(reverse("users:detail", args=[admin_user.pk]), {"action": "deactivate"})
    admin_user.refresh_from_db()
    assert admin_user.is_active
    with pytest.raises(accounts.AccountError, match="last Admin"):
        accounts.set_role(admin_user, "Viewer")


def test_role_change_and_deactivation_sign_out(client, admin_user, roles):
    victim = make_user("ops", role="Operator", email="ops@example.com")
    other = client.__class__()
    other.force_login(victim)
    assert other.get(reverse("fleet:dashboard")).status_code == 200
    client.force_login(admin_user)
    client.post(reverse("users:detail", args=[victim.pk]),
                {"action": "save", "first_name": "", "last_name": "", "email": "ops@example.com", "role": "Viewer"})  # fmt: skip
    assert accounts.role_of(victim) == "Viewer"
    client.post(reverse("users:detail", args=[victim.pk]), {"action": "deactivate"})
    assert other.get(reverse("fleet:dashboard")).status_code == 302  # session dead
    assert AuditLog.objects.filter(action="user.deactivate", target="ops").exists()


def test_two_admins_can_demote_each_other_but_not_the_last(admin_user, roles):
    second = make_user("boss", role="Admin")
    accounts.set_role(second, "Viewer", actor=admin_user)
    with pytest.raises(accounts.AccountError):
        accounts.set_role(admin_user, "Operator", actor=second)


def test_admin_reset_link_shown_once(client, admin_user, roles):
    victim = make_user("ops")
    client.force_login(admin_user)
    page = client.post(reverse("users:detail", args=[victim.pk]), {"action": "reset_link"}).content
    assert b"/password/reset/" in page
    assert b"/password/reset/" not in client.get(reverse("users:detail", args=[victim.pk])).content


def test_only_admins_manage_users(client, operator):
    client.force_login(operator)
    assert client.get(reverse("users:list")).status_code == 403
    assert client.get(reverse("users:invite")).status_code == 403


# ---------------------------------------------------------------- own account


def test_change_own_password_keeps_this_session_only(client, roles):
    user = make_user("me")
    elsewhere = client.__class__()
    elsewhere.force_login(user)
    client.force_login(user)
    resp = client.post(reverse("account"), {"change_password": "1", "old_password": GOOD_PASSWORD,
                                            "new_password1": "another-good-pass-7", "new_password2": "another-good-pass-7"})  # fmt: skip
    assert resp.status_code == 302
    assert client.get(reverse("account")).status_code == 200
    assert elsewhere.get(reverse("fleet:dashboard")).status_code == 302


def test_profile_email_must_be_unique(client, roles):
    make_user("a", email="a@example.com")
    me = make_user("me")
    client.force_login(me)
    resp = client.post(
        reverse("account"), {"first_name": "", "last_name": "", "email": "A@example.com"}
    )
    assert b"Another account already uses this email" in resp.content


def test_invite_page_while_signed_in_uses_auth_layout(client, admin_user):
    _, raw = Invitation.issue("x@example.com", "Viewer", admin_user)
    client.force_login(admin_user)
    page = client.get(reverse("accept_invite", args=[raw])).content.decode()
    assert "Join" in page and "separate account" in page and 'class="sidebar"' not in page
