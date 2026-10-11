import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Environment(models.TextChoices):
    PRODUCTION = "production", "Production"
    STAGING = "staging", "Staging"
    DEVELOPMENT = "development", "Development"


class Health(models.TextChoices):
    PENDING = "pending", "Awaiting agent"
    ONLINE = "online", "Online"
    LATE = "late", "Late"
    OFFLINE = "offline", "Offline"
    DISABLED = "disabled", "Disabled"


class ServerGroup(models.Model):
    """Free-form grouping (e.g. `api`, `workers`, `client-x`) — the Ansible inventory idea."""

    name = models.SlugField(max_length=60, unique=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Server(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True)
    environment = models.CharField(
        max_length=20, choices=Environment.choices, default=Environment.PRODUCTION
    )
    groups = models.ManyToManyField(ServerGroup, blank=True, related_name="servers")
    description = models.CharField(max_length=200, blank=True)
    is_enabled = models.BooleanField(
        default=True, help_text="Disabled servers get no commands and raise no alerts."
    )

    # Pushed to the agent in every heartbeat response, so changing them here retunes the
    # agent within one cycle — no SSH needed.
    report_interval = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(5), MaxValueValidator(3600)],
        help_text="Seconds between metric reports.",
    )
    command_poll_interval = models.PositiveIntegerField(
        default=5,
        validators=[MinValueValidator(2), MaxValueValidator(300)],
        help_text="Seconds between checks for queued commands.",
    )

    # Only the SHA-256 of the agent token is stored. The token is 256 bits of randomness,
    # so a fast hash is sufficient (no dictionary to attack) and lookup stays one index hit.
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    token_prefix = models.CharField(max_length=8, editable=False)

    # Reported by the agent.
    hostname = models.CharField(max_length=255, blank=True)
    platform = models.CharField(max_length=255, blank=True)
    agent_version = models.CharField(max_length=32, blank=True)
    cpu_count = models.PositiveIntegerField(null=True, blank=True)
    memory_total = models.BigIntegerField(null=True, blank=True)
    uptime_seconds = models.BigIntegerField(null=True, blank=True)
    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    capabilities = models.JSONField(default=dict, blank=True)
    services = models.JSONField(default=list, blank=True)
    disks = models.JSONField(default=list, blank=True)

    # Latest snapshot, denormalised so the dashboard is one query with no subqueries.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    cpu_percent = models.FloatField(null=True, blank=True)
    memory_percent = models.FloatField(null=True, blank=True)
    disk_percent = models.FloatField(null=True, blank=True)
    load_1 = models.FloatField(null=True, blank=True)
    net_rx_bps = models.FloatField(null=True, blank=True)
    net_tx_bps = models.FloatField(null=True, blank=True)

    # The health the sweep last acted on. Display uses `health` (computed); this field only
    # exists so offline alerts fire on the transition, not on every sweep.
    last_health = models.CharField(max_length=20, choices=Health.choices, default=Health.PENDING)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["environment", "name"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(report_interval__gte=5, report_interval__lte=3600),
                name="server_report_interval_range",
            ),
            models.CheckConstraint(
                condition=Q(command_poll_interval__gte=2, command_poll_interval__lte=300),
                name="server_poll_interval_range",
            ),
        ]

    def __str__(self):
        return self.name

    # -- health -----------------------------------------------------------------------

    def health_at(self, now: datetime) -> str:
        if not self.is_enabled:
            return Health.DISABLED
        if self.last_seen_at is None:
            return Health.PENDING
        age = (now - self.last_seen_at).total_seconds()
        grace = settings.MONITOR_GRACE_SECONDS
        if age <= self.report_interval * settings.MONITOR_LATE_AFTER_INTERVALS + grace:
            return Health.ONLINE
        if age <= self.report_interval * settings.MONITOR_OFFLINE_AFTER_INTERVALS + grace:
            return Health.LATE
        return Health.OFFLINE

    @property
    def health(self) -> str:
        return self.health_at(timezone.now())

    @property
    def health_label(self) -> str:
        return Health(self.health).label

    @property
    def actions(self) -> list[dict]:
        return list(self.capabilities.get("actions", []))

    @property
    def action_names(self) -> set[str]:
        return {a.get("name") for a in self.actions}

    @property
    def log_names(self) -> list[str]:
        return list(self.capabilities.get("logs", []))

    @property
    def restartable_services(self) -> list[str]:
        return [s["name"] for s in self.services if s.get("restartable")]

    @property
    def shell_enabled(self) -> bool:
        return bool(self.capabilities.get("shell"))


class MetricSample(models.Model):
    """One heartbeat's numbers. Pruned after MONITOR_METRIC_RETENTION_DAYS."""

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="samples")
    # Receipt time on main, not the agent's clock: a skewed agent must not reorder history.
    recorded_at = models.DateTimeField(default=timezone.now)
    cpu_percent = models.FloatField()
    memory_percent = models.FloatField()
    disk_percent = models.FloatField()
    load_1 = models.FloatField(null=True)
    net_rx_bps = models.FloatField(null=True)
    net_tx_bps = models.FloatField(null=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [
            # Chart and CPU-average queries: WHERE server_id = ? ORDER BY recorded_at DESC.
            models.Index(fields=["server", "-recorded_at"], name="sample_server_time"),
            # Retention prune: WHERE recorded_at < ?.
            models.Index(fields=["recorded_at"], name="sample_time"),
        ]

    def __str__(self):
        return f"{self.server_id} @ {self.recorded_at:%Y-%m-%d %H:%M:%S}"

    @classmethod
    def retention_cutoff(cls) -> datetime:
        return timezone.now() - timedelta(days=settings.MONITOR_METRIC_RETENTION_DAYS)
