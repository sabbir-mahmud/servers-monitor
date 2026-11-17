"""Creating accounts and adding people to them. Views, sign-up, invitations and SSO all
go through here so the same rules and audit rows apply everywhere."""

from django.db import transaction

from apps.audit.services import record

from .middleware import SESSION_KEY
from .models import Account, Membership
from .roles import Kind, Role


class TenantError(Exception):
    """A refused change, with a message fit to show the person who tried."""


@transaction.atomic
def create_account(request_or_user, user, name: str, kind: str) -> Account:
    if kind not in Kind.values:
        raise TenantError("Choose Parent, Solo developer or Company.")
    name = (name or "").strip() or f"{user.display_name}'s account"
    account = Account.objects.create(name=name[:100], kind=kind, created_by=user)
    Membership.objects.create(account=account, user=user, role=Role.OWNER)
    record(request_or_user, "account.create", target=account.name, account=account, kind=kind)
    return account


def switch_to(request, account: Account) -> None:
    request.session[SESSION_KEY] = str(account.pk)


@transaction.atomic
def join(invitation, user) -> Membership:
    """Accept `invitation` as `user` (new or existing). Re-checks inside the transaction:
    two tabs must not both use one invite."""
    from django.utils import timezone

    from apps.users.models import Invitation

    invite = Invitation.objects.select_for_update().select_related("account").get(pk=invitation.pk)
    if not invite.is_usable:
        raise TenantError("This invitation was already used, revoked or has expired.")
    if invite.email.lower() != (user.email or "").lower():
        raise TenantError("This invitation is for a different email address.")
    if invite.role not in invite.account.roles:
        raise TenantError("This invitation's role no longer exists for the account.")
    membership, created = Membership.objects.get_or_create(
        account=invite.account, user=user,
        defaults={"role": invite.role, "invited_by": invite.invited_by},
    )  # fmt: skip
    if not created:
        raise TenantError(f"You're already a member of {invite.account.name}.")
    invite.accepted_at, invite.accepted_by = timezone.now(), user
    invite.save(update_fields=["accepted_at", "accepted_by"])
    return membership
