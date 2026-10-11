import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.fields import EncryptedTextField


class User(AbstractUser):
    """Custom from day one so adding fields later never needs a swap migration."""

    # Self sign-up (off by default) creates inactive accounts an Admin must approve.
    pending_approval = models.BooleanField(default=False)

    class Meta(AbstractUser.Meta):
        swappable = "AUTH_USER_MODEL"
        constraints = [
            # Password resets go to an address: it must name exactly one account.
            models.UniqueConstraint(
                Lower("email"), condition=~Q(email=""), name="user_email_unique_ci"
            ),
        ]

    @property
    def display_name(self) -> str:
        return self.get_full_name() or self.get_username()

    @property
    def initials(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0] for p in parts)[:2].upper()
        return self.get_username()[:2].upper()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Invitation(models.Model):
    """A single-use link that lets someone create their own account with a preset role.
    Only a SHA-256 of the token is stored; the link is shown once (and emailed if SMTP is
    configured)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    email = models.EmailField()
    role = models.CharField(max_length=20)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invitation",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invitation for {self.email}"

    @classmethod
    def issue(cls, email: str, role: str, invited_by) -> tuple[Invitation, str]:
        raw = secrets.token_urlsafe(32)
        invite = cls.objects.create(
            email=email.strip(),
            role=role,
            token_hash=hash_token(raw),
            invited_by=invited_by,
            expires_at=timezone.now() + timedelta(days=settings.INVITATION_DAYS),
        )
        return invite, raw

    @classmethod
    def find(cls, raw: str) -> Invitation | None:
        if not raw or len(raw) > 100:
            return None
        return cls.objects.filter(token_hash=hash_token(raw)).first()

    @property
    def state(self) -> str:
        if self.accepted_at:
            return "accepted"
        if self.revoked_at:
            return "revoked"
        if self.expires_at <= timezone.now():
            return "expired"
        return "pending"

    @property
    def is_usable(self) -> bool:
        return self.state == "pending"


class TOTPDevice(models.Model):
    """A user's authenticator app. Unconfirmed until they've entered one valid code."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="totp"
    )
    secret = EncryptedTextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_used_step = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"2FA for {self.user}"

    @property
    def is_active(self) -> bool:
        return self.confirmed_at is not None


class RecoveryCode(models.Model):
    """Single-use backup codes, stored only as SHA-256 hashes."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recovery_codes"
    )
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Recovery code for {self.user}"

    @staticmethod
    def normalise(code: str) -> str:
        return "".join(ch for ch in code.lower() if ch.isalnum())


class SSOIdentity(models.Model):
    """Which single sign-on (OIDC) subject is which local account (see apps/users/sso.py)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sso"
    )
    subject = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user} ↔ {self.subject}"
