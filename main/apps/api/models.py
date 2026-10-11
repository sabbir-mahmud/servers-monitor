"""Personal API tokens. Only a SHA-256 of each token is stored: it's 256 random bits, so a
fast hash is enough, and the token itself is shown once, when it's created."""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

PREFIX = "smt_"


class Scope(models.TextChoices):
    READ = "read", "Read only"
    OPERATE = "operate", "Read and run actions"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class ApiToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=100)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField(max_length=12, editable=False)
    scope = models.CharField(max_length=8, choices=Scope.choices, default=Scope.READ)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def issue(cls, owner, name: str, scope: str, expires_at=None) -> tuple[ApiToken, str]:
        """Create a token. Returns (token, the raw value to show once)."""
        raw = PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(owner=owner, name=name, scope=scope, expires_at=expires_at,
                                   token_hash=hash_token(raw), prefix=raw[: len(PREFIX) + 6])  # fmt: skip
        return token, raw

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    @property
    def can_operate(self) -> bool:
        return self.scope == Scope.OPERATE
