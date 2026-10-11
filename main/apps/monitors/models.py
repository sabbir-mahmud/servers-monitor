"""Application monitors: HTTP(S) health checks the dashboard runs on a schedule.

Checks go out from the dashboard's relay process (`manage.py relay`), so they cost no
Redis commands and see your apps the way users do. For endpoints only reachable from
inside a server (localhost health URLs), use an agent `http` service instead.
"""

import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

STATUS_SPEC = re.compile(r"^\s*\d{3}(\s*-\s*\d{3})?(\s*,\s*\d{3}(\s*-\s*\d{3})?)*\s*$")


def parse_status_spec(spec: str) -> list[tuple[int, int]]:
    """ "200-299,301" -> [(200, 299), (301, 301)]."""
    ranges = []
    for part in spec.split(","):
        lo, _, hi = part.strip().partition("-")
        ranges.append((int(lo), int(hi or lo)))
    return ranges


def validate_status_spec(value: str) -> None:
    if not STATUS_SPEC.match(value or ""):
        raise ValidationError("Use codes and ranges like 200-399 or 200,204,301.")
    for lo, hi in parse_status_spec(value):
        if not (100 <= lo <= hi <= 599):
            raise ValidationError("Status codes must be between 100 and 599, low before high.")


class MonitorStatus(models.TextChoices):
    PENDING = "pending", "Checking…"
    UP = "up", "Up"
    DOWN = "down", "Down"
    PAUSED = "paused", "Paused"


class Method(models.TextChoices):
    GET = "GET", "GET"
    HEAD = "HEAD", "HEAD"


class Monitor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True)
    url = models.URLField(max_length=500, help_text="The health or home page URL, http(s) only.")
    method = models.CharField(max_length=4, choices=Method.choices, default=Method.GET)
    interval_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(30), MaxValueValidator(3600)],
        help_text="How often to check, in seconds (30-3600).",
    )
    timeout_seconds = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(60)]
    )
    expected_status = models.CharField(
        max_length=60,
        default="200-399",
        validators=[validate_status_spec],
        help_text="Status codes that count as up, e.g. 200-399 or 200,204.",
    )
    keyword = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional: the response body must contain this text (GET only).",
    )
    failure_threshold = models.PositiveSmallIntegerField(
        default=2,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Consecutive failed checks before it counts as down (failures are "
        "re-checked after 15 s, so one blip never pages you).",
    )
    verify_tls = models.BooleanField(
        default=True, help_text="Fail on invalid certificates. Turn off only for self-signed."
    )
    server = models.ForeignKey(
        "fleet.Server",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="monitors",
        help_text="Optional: the server this app runs on, shown together on its page.",
    )
    is_enabled = models.BooleanField(default=True)

    # Current state, written by the checker.
    status = models.CharField(
        max_length=10, choices=MonitorStatus.choices, default=MonitorStatus.PENDING
    )
    status_since = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_response_ms = models.PositiveIntegerField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now, db_index=True)
    tls_expires_at = models.DateTimeField(null=True, blank=True)
    tls_checked_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(interval_seconds__gte=30, interval_seconds__lte=3600),
                name="monitor_interval_range",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.keyword and self.method == Method.HEAD:
            raise ValidationError({"keyword": "A HEAD request has no body to search; use GET."})

    @property
    def expected_ranges(self) -> list[tuple[int, int]]:
        return parse_status_spec(self.expected_status)

    @property
    def tls_days_left(self) -> int | None:
        if self.tls_expires_at is None:
            return None
        return (self.tls_expires_at - timezone.now()).days

    @property
    def display_status(self) -> str:
        return MonitorStatus.PAUSED if not self.is_enabled else self.status


class MonitorCheck(models.Model):
    """One check result. Pruned with metric samples (MONITOR_METRIC_RETENTION_DAYS)."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="checks")
    checked_at = models.DateTimeField(default=timezone.now)
    ok = models.BooleanField()
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["monitor", "-checked_at"], name="check_monitor_time")]

    def __str__(self):
        return f"{self.monitor_id} @ {self.checked_at:%Y-%m-%d %H:%M:%S} {'ok' if self.ok else 'failed'}"

    @classmethod
    def retention_cutoff(cls):
        return timezone.now() - timedelta(days=settings.MONITOR_METRIC_RETENTION_DAYS)
