"""People operations: roles in an account, the last-owner guard, reset and invitation
links, email. Roles are memberships (apps/tenants); a user's login itself (password,
email, two-factor, active) is global and can only be managed by someone who manages
every account that user belongs to, or by a superuser."""

import logging
import threading

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.tenants.models import Membership
from apps.tenants.permissions import membership_of
from apps.tenants.roles import MANAGER_ROLES, ROLE_HELP, Role

logger = logging.getLogger(__name__)

__all__ = ["ROLE_HELP", "AccountError", "Role"]


class AccountError(Exception):
    """A refused account change, with a message fit to show the person who tried."""


def check_can_manage(actor, person) -> None:
    """Admins manage ordinary accounts; only a superuser may touch a superuser. Without
    this, an Admin could reset a superuser's password (or swap their email and use
    "forgot password") and gain Django admin, i.e. the whole database."""
    if person.is_superuser and not actor.is_superuser:
        raise AccountError("Only a superuser can change a superuser's account.")


def can_manage_login(actor, person, account) -> bool:
    """May `actor` reset `person`'s password, two-factor, email or deactivate them? Only
    when every account `person` belongs to is `account` and `actor` manages it: otherwise
    an Admin of one family could take over the login someone uses for their company."""
    if actor.is_superuser:
        return True
    if person.is_superuser or person.pk == actor.pk:
        return False
    actor_m = membership_of(actor)
    if actor_m is None or actor_m.account_id != account.pk or actor_m.role not in MANAGER_ROLES:
        return False
    accounts = set(Membership.objects.filter(user=person).values_list("account_id", flat=True))
    return accounts == {account.pk}


def role_of(user, account=None) -> str | None:
    """The user's role in `account` (default: the one bound for this request)."""
    if account is None:
        m = membership_of(user)
        return m.role if m is not None else None
    return (
        Membership.objects.filter(user=user, account=account).values_list("role", flat=True).first()
    )


def owners(account):
    return Membership.objects.filter(account=account, role=Role.OWNER, user__is_active=True)


def is_last_owner(membership: Membership) -> bool:
    return membership.role == Role.OWNER and owners(membership.account).count() <= 1


@transaction.atomic
def set_role(membership: Membership, role: str, *, actor=None) -> None:
    """Change someone's role in their account, with the guards every path needs."""
    account = membership.account
    if role not in account.roles:
        raise AccountError(f"{account.get_kind_display()} accounts have no {role} role.")
    if role == membership.role:
        return
    if actor is not None:
        if actor.pk == membership.user_id:
            raise AccountError("You can't change your own role. Ask another owner or admin.")
        mine = role_of(actor, account)
        if (
            (Role.OWNER in (role, membership.role))
            and mine != Role.OWNER
            and not actor.is_superuser
        ):
            raise AccountError("Only an owner can make someone an owner, or change an owner.")
    if is_last_owner(membership):
        raise AccountError("This is the account's only owner. Make someone else owner first.")
    membership.role = role
    membership.save(update_fields=["role"])


@transaction.atomic
def remove_member(membership: Membership, *, actor=None) -> None:
    if is_last_owner(membership):
        raise AccountError("This is the account's only owner. Make someone else owner first.")
    if actor is not None and actor.pk != membership.user_id:
        mine = role_of(actor, membership.account)
        if membership.role == Role.OWNER and mine != Role.OWNER and not actor.is_superuser:
            raise AccountError("Only an owner can remove an owner.")
    membership.delete()


@transaction.atomic
def set_active(user, active: bool, *, actor=None) -> None:
    if not active:
        if actor is not None and actor.pk == user.pk:
            raise AccountError("You can't deactivate your own account.")
        for m in Membership.objects.filter(user=user, role=Role.OWNER).select_related("account"):
            if is_last_owner(m):
                raise AccountError(
                    f"{user.display_name} is the only owner of {m.account.name}. "
                    "Make someone else owner first."
                )
    user.is_active = active
    if active:
        user.pending_approval = False
    # An inactive user's existing sessions stop working on their next request
    # (ModelBackend refuses inactive users when loading the session).
    user.save(update_fields=["is_active", "pending_approval"])


# --------------------------------------------------------------------------- links


def absolute_url(request, path: str) -> str:
    """Links in emails and copy boxes. DASHBOARD_URL wins: the request's host may be an
    internal name the recipient can't open."""
    if settings.DASHBOARD_URL:
        return settings.DASHBOARD_URL.rstrip("/") + path
    return request.build_absolute_uri(path)


def reset_link(request, user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return absolute_url(request, reverse("password_reset_confirm", args=[uid, token]))


def invite_link(request, raw_token: str) -> str:
    return absolute_url(request, reverse("accept_invite", args=[raw_token]))


# --------------------------------------------------------------------------- email


def _send(subject: str, template: str, context: dict, to: str) -> None:
    try:
        context = {"SITE_NAME": settings.SITE_NAME, **context}
        text = render_to_string(f"emails/{template}.txt", context)
        html = render_to_string(f"emails/{template}.html", context)
        message = EmailMultiAlternatives(f"{settings.SITE_NAME}: {subject}", text, to=[to])
        message.attach_alternative(html, "text/html")
        message.send()
    except Exception:  # never let mail problems surface to (or time) the requester
        logger.exception("Could not send %s email", template)
    finally:
        close_old_connections()


def send_async(subject: str, template: str, context: dict, to: str) -> None:
    """Send from a thread: the page answers in the same time whether or not an email went
    out, so response timing can't reveal which addresses have accounts."""
    threading.Thread(target=_send, args=(subject, template, context, to), daemon=True).start()


def send_reset_email(request, user) -> None:
    send_async(
        "reset your password",
        "password_reset",
        {
            "user": user,
            "link": reset_link(request, user),
            "hours": settings.PASSWORD_RESET_TIMEOUT // 3600,
        },
        user.email,
    )


def send_invite_email(request, invitation, raw_token: str) -> None:
    send_async(
        "you're invited",
        "invitation",
        {
            "invitation": invitation,
            "link": invite_link(request, raw_token),
            "inviter": invitation.invited_by,
            "days": settings.INVITATION_DAYS,
        },
        invitation.email,
    )
