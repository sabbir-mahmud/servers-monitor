"""Account operations: roles, the last-admin guard, reset and invitation links, email."""

import logging
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.db import close_old_connections, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .roles import ROLE_MATRIX

logger = logging.getLogger(__name__)

ROLES = list(ROLE_MATRIX)  # Viewer, Operator, Admin
ROLE_HELP = {
    "Viewer": "Sees everything except the audit log. Can't change anything.",
    "Operator": "Viewer, plus runs actions, acknowledges alerts, mutes and edits apps.",
    "Admin": "Everything: servers, credentials, users, notifications, shell, audit log.",
}


class AccountError(Exception):
    """A refused account change, with a message fit to show the person who tried."""


def check_can_manage(actor, person) -> None:
    """Admins manage ordinary accounts; only a superuser may touch a superuser. Without
    this, an Admin could reset a superuser's password (or swap their email and use
    "forgot password") and gain Django admin, i.e. the whole database."""
    if person.is_superuser and not actor.is_superuser:
        raise AccountError("Only a superuser can change a superuser's account.")


def role_of(user) -> str | None:
    if user.is_superuser:
        return "Admin"
    names = set(user.groups.values_list("name", flat=True))
    for role in reversed(ROLES):  # highest wins if someone has several
        if role in names:
            return role
    return None


def _admins():
    from django.db.models import Q

    User = get_user_model()
    return (
        User.objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name="Admin"))
        .distinct()
    )


def is_last_admin(user) -> bool:
    return role_of(user) == "Admin" and user.is_active and _admins().count() <= 1


@transaction.atomic
def set_role(user, role: str | None, *, actor=None) -> None:
    if role is not None and role not in ROLES:
        raise AccountError("Unknown role.")
    if actor is not None and actor.pk == user.pk and role != role_of(user):
        raise AccountError("You can't change your own role. Ask another Admin.")
    if role != "Admin" and is_last_admin(user):
        raise AccountError("This is the last Admin. Make someone else an Admin first.")
    if user.is_superuser and role != "Admin":
        raise AccountError("Superusers are always Admins. Change that in Django admin.")
    user.groups.remove(*Group.objects.filter(name__in=ROLES))
    if role:
        user.groups.add(Group.objects.get(name=role))


@transaction.atomic
def set_active(user, active: bool, *, actor=None) -> None:
    if not active:
        if actor is not None and actor.pk == user.pk:
            raise AccountError("You can't deactivate your own account.")
        if is_last_admin(user):
            raise AccountError("This is the last Admin. Make someone else an Admin first.")
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
