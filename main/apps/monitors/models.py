"""Application monitors: checks the dashboard runs on a schedule (HTTP, TCP, ping, DNS,
domain expiry) and heartbeats that servers send it (cron jobs via `smctl ping`).

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

from apps.core.fields import EncryptedTextField

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
    DEGRADED = "degraded", "Slow"
    DOWN = "down", "Down"
    PAUSED = "paused", "Paused"


class Kind(models.TextChoices):
    HTTP = "http", "HTTP(S)"
    TCP = "tcp", "TCP port"
    PING = "ping", "Ping"
    DNS = "dns", "DNS record"
    DOMAIN = "domain", "Domain expiry"
    HEARTBEAT = "heartbeat", "Heartbeat (cron job)"


class Method(models.TextChoices):
    GET = "GET", "GET"
    HEAD = "HEAD", "HEAD"
    POST = "POST", "POST"
    PUT = "PUT", "PUT"


class DnsType(models.TextChoices):
    A = "A", "A"
    AAAA = "AAAA", "AAAA"
    CNAME = "CNAME", "CNAME"
    MX = "MX", "MX"
    TXT = "TXT", "TXT"
    NS = "NS", "NS"


HOST_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)


def is_host(value: str) -> bool:
    import ipaddress

    value = (value or "").strip("[]")
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(HOST_RE.match(value))


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class Monitor(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.HTTP)
    url = models.URLField(
        max_length=500, blank=True, help_text="The health or home page URL, http(s) only."
    )
    # Host (TCP, ping, DNS), domain name (domain expiry) or ping name (heartbeat).
    target = models.CharField(max_length=253, blank=True)
    # Per-kind settings (see FIELDS_BY_KIND in forms.py); secrets (request headers) apart.
    options = models.JSONField(default=dict, blank=True)
    secret_options = EncryptedTextField(blank=True, default="")
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

    # Where it's checked from (HTTP and TCP): this dashboard and/or chosen agents, which run
    # the probe and send results with their heartbeats. Down needs `quorum` locations to agree.
    check_from_dashboard = models.BooleanField(
        default=True, help_text="Check from this dashboard's own network."
    )
    probe_servers = models.ManyToManyField(
        "fleet.Server",
        blank=True,
        related_name="probe_monitors",
        help_text="Also check from these servers (agents with probes on). Internal URLs work.",
    )
    quorum = models.PositiveSmallIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Down only when at least this many locations fail.",
    )
    # Latest result per location: {"dashboard" | server id: {ok, degraded, failures, at, ...}}.
    location_states = models.JSONField(default=dict, blank=True)

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
    domain_expires_at = models.DateTimeField(null=True, blank=True)
    # Heartbeats: when the job last said "start" (its runtime is measured from here).
    run_started_at = models.DateTimeField(null=True, blank=True)

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

    def save(self, *args, **kwargs):
        if self._state.adding and self.kind == Kind.HEARTBEAT:
            # The first ping is due one period (plus grace) from now, not immediately.
            period = int(self.opt("period_seconds", 86400)) + int(self.opt("grace_seconds", 3600))
            self.next_check_at = timezone.now() + timedelta(seconds=period)
        super().save(*args, **kwargs)

    def clean(self):
        errors = {}
        if self.kind == Kind.HTTP:
            if not self.url:
                errors["url"] = "Enter the URL to check."
            if self.keyword and self.method == Method.HEAD:
                errors["keyword"] = "A HEAD request has no body to search; use GET."
        elif self.kind == Kind.HEARTBEAT:
            if not SLUG_RE.match(self.target or ""):
                errors["target"] = "Use lowercase letters, digits, dots, dashes or underscores."
            if self.server_id is None:
                errors["server"] = "Choose the server whose cron job pings this."
            elif (
                Monitor.objects.filter(
                    kind=Kind.HEARTBEAT, server_id=self.server_id, target=self.target
                )
                .exclude(pk=self.pk)
                .exists()
            ):
                errors["target"] = "That server already has a heartbeat with this name."
        elif not is_host(self.target):
            errors["target"] = "Enter a host name or IP address, like db.example.com."
        if errors:
            raise ValidationError(errors)

    def opt(self, key, default=None):
        return (self.options or {}).get(key, default)

    @property
    def headers(self) -> dict:
        import json

        try:
            return json.loads(self.secret_options or "{}").get("headers", {})
        except ValueError:
            return {}

    @property
    def supports_locations(self) -> bool:
        return self.kind in (Kind.HTTP, Kind.TCP)

    @property
    def short_kind(self) -> str:
        return {Kind.HEARTBEAT: "cron", Kind.DOMAIN: "domain"}.get(self.kind, self.kind)

    @property
    def is_heartbeat(self) -> bool:
        return self.kind == Kind.HEARTBEAT

    @property
    def where(self) -> str:
        """What is being checked, for lists ("https://…", "db:5432", "ping web-01")."""
        if self.kind == Kind.HTTP:
            return self.url
        if self.kind == Kind.TCP:
            return f"{self.target}:{self.opt('port', '')}"
        if self.kind == Kind.DNS:
            return f"{self.opt('record_type', 'A')} {self.target}"
        if self.kind == Kind.HEARTBEAT:
            return f"smctl ping {self.target}"
        return self.target

    @property
    def expected_ranges(self) -> list[tuple[int, int]]:
        return parse_status_spec(self.expected_status)

    @property
    def domain_days_left(self) -> int | None:
        if self.domain_expires_at is None:
            return None
        return (self.domain_expires_at - timezone.now()).days

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
    degraded = models.BooleanField(default=False)  # up, but slower than the monitor allows
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)
    # "" = this dashboard; otherwise the id of the server whose agent ran the check.
    location = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["monitor", "-checked_at"], name="check_monitor_time")]

    def __str__(self):
        return f"{self.monitor_id} @ {self.checked_at:%Y-%m-%d %H:%M:%S} {'ok' if self.ok else 'failed'}"

    @classmethod
    def retention_cutoff(cls):
        return timezone.now() - timedelta(days=settings.MONITOR_METRIC_RETENTION_DAYS)


class MonitorDaily(models.Model):
    """A monitor's day (in TIME_ZONE) of outcome checks, kept ROLLUP_RETENTION_DAYS: long
    uptime history for status pages after raw checks are pruned."""

    monitor = models.ForeignKey(Monitor, on_delete=models.CASCADE, related_name="days")
    day = models.DateField()
    up = models.PositiveIntegerField()
    total = models.PositiveIntegerField()
    avg_ms = models.FloatField(null=True)

    class Meta:
        ordering = ["-day"]
        constraints = [
            models.UniqueConstraint(fields=["monitor", "day"], name="monitor_daily_one_per_day")
        ]

    def __str__(self):
        return f"{self.monitor_id} {self.day}: {self.up}/{self.total}"
