import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class AlertKind(models.TextChoices):
    OFFLINE = "offline", "Server offline"
    CPU = "cpu", "High CPU"
    MEMORY = "memory", "High memory"
    DISK = "disk", "Disk filling up"
    SERVICE = "service", "Service down"
    APP_DOWN = "app_down", "Application down"
    CERT = "cert", "Certificate expiring"


class Severity(models.TextChoices):
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"


class Alert(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    # Exactly one of these is set: server alerts come from heartbeats, monitor alerts from
    # application health checks.
    server = models.ForeignKey(
        "fleet.Server", null=True, blank=True, on_delete=models.CASCADE, related_name="alerts"
    )
    monitor = models.ForeignKey(
        "monitors.Monitor", null=True, blank=True, on_delete=models.CASCADE, related_name="alerts"
    )
    kind = models.CharField(max_length=20, choices=AlertKind.choices)
    # Distinguishes several open alerts of one kind on one server (the service name).
    subject = models.CharField(max_length=100, blank=True, default="")
    severity = models.CharField(max_length=10, choices=Severity.choices)
    message = models.CharField(max_length=300)
    value = models.FloatField(null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            # The dedupe rule lives in the database: two concurrent heartbeats cannot open
            # the same alert twice.
            models.UniqueConstraint(
                fields=["server", "kind", "subject"],
                condition=Q(resolved_at__isnull=True),
                name="alert_one_open_per_subject",
            ),
            models.UniqueConstraint(
                fields=["monitor", "kind"],
                condition=Q(resolved_at__isnull=True, monitor__isnull=False),
                name="alert_one_open_per_monitor_kind",
            ),
            models.CheckConstraint(
                condition=Q(server__isnull=False) ^ Q(monitor__isnull=False),
                name="alert_server_xor_monitor",
            ),
        ]
        indexes = [
            models.Index(
                fields=["server"],
                condition=Q(resolved_at__isnull=True),
                name="alert_open_by_server",
            ),
            models.Index(fields=["-opened_at"], name="alert_opened"),
        ]

    def __str__(self):
        return f"{self.server or self.monitor} {self.get_kind_display()} {self.subject}".strip()

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None
