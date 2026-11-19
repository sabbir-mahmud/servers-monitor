"""Accounts: the unit of ownership. Every server, monitor, channel, job, schedule, status
page, alert, API token and audit row belongs to exactly one account, and people only
ever see the accounts they're members of.

- Account    a family, a solo developer or a company
- Membership a user's role in one account (a user can belong to several)
- Person     someone whose devices the account looks after: a child in a family, a
             staff member in a company. Optional login (`user`).
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.text import slugify

from .roles import Kind, Role, has_feature, permissions_for, roles_for


class AccountQuerySet(models.QuerySet):
    def for_user(self, user):
        return self.filter(memberships__user=user)


class ComputerPairing(models.TextChoices):
    """How a Windows, Mac or Linux computer may pair with this account."""

    APP = "app", "With the NexGuard app"
    APP_OR_COMMAND = "app_or_command", "With the NexGuard app or a one-line command"


# What the desktop app sends as "client" when it pairs (desktop-app/, `nexguard pair --via app`).
DESKTOP_APP_CLIENT = "desktop-app"


class Account(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=100)
    # Stable handle for settings (OIDC_ACCOUNT) and, later, URLs. Never shown as a secret.
    slug = models.SlugField(max_length=60, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    # The app by default: it shows the person what's shared, keeps the agent's log in view
    # and installs without a terminal. The one-line command is the opt-in alternative.
    computer_pairing = models.CharField(
        max_length=20, choices=ComputerPairing.choices, default=ComputerPairing.APP
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AccountQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = unique_slug(self.name)
        super().save(*args, **kwargs)

    @property
    def computers_need_app(self) -> bool:
        return self.computer_pairing == ComputerPairing.APP

    def has(self, feature: str) -> bool:
        return has_feature(self.kind, feature)

    @property
    def roles(self) -> list[Role]:
        return roles_for(self.kind)

    @property
    def people_label(self) -> str:
        return "People" if self.kind == Kind.FAMILY else "Staff"

    @property
    def machine_label(self) -> str:
        """What the agent's machines are called: a solo developer's laptop and VPS are
        "devices"; a company runs "servers"."""
        return "device" if self.kind == Kind.SOLO else "server"

    @property
    def person_label(self) -> str:
        return "person" if self.kind == Kind.FAMILY else "staff member"


def unique_slug(name: str) -> str:
    base = slugify(name)[:50] or "account"
    slug, n = base, 1
    while Account.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


class Membership(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["account", "user"], name="membership_one_per_user"),
        ]

    def __str__(self):
        return f"{self.user} · {self.role} in {self.account}"

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.account.kind, self.role)

    @property
    def role_label(self) -> str:
        return Role(self.role).label

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER


class PersonKind(models.TextChoices):
    CHILD = "child", "Child"
    PARTNER = "partner", "Spouse or partner"
    RELATIVE = "relative", "Other family"
    STAFF = "staff", "Staff member"


# Who a family account can add; a company only adds staff.
FAMILY_KINDS = (PersonKind.CHILD, PersonKind.PARTNER, PersonKind.RELATIVE)


class Person(models.Model):
    """Whose devices these are. Devices point here (phase 2); alerts and the parent home
    page are grouped by person."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="people")
    kind = models.CharField(max_length=10, choices=PersonKind.choices)
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, help_text="Optional. Staff: their work address.")
    notes = models.CharField(max_length=200, blank=True)
    # A staff member who also signs in (to see their own devices). Never required.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "account",
                Lower("name"),
                name="person_name_unique_per_account",
                violation_error_message="Someone in this account already has that name.",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["child", "partner", "relative", "staff"]),
                name="person_kind_valid",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def initials(self) -> str:
        parts = self.name.split()
        return "".join(p[0] for p in parts[:2]).upper() or "?"
