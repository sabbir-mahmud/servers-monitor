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


SEVERITY_RANK = {Severity.WARNING: 1, Severity.CRITICAL: 2}


class ChannelKind(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    WEBHOOK = "webhook", "Webhook (Slack, Discord, Teams)"


class Channel(models.Model):
    """Where alert notifications go, and which alerts. Sent by the relay process from a
    database outbox (see delivery.py): retried on failure, never sent twice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=80, unique=True)
    kind = models.CharField(max_length=10, choices=ChannelKind.choices)
    is_enabled = models.BooleanField(default=True)

    # Telegram: a bot from @BotFather and the chat (person or group) it writes to.
    telegram_token = models.CharField(max_length=100, blank=True)
    telegram_chat_id = models.CharField(max_length=40, blank=True)
    # Webhook: an incoming-webhook URL. Secret too (whoever has it can post).
    webhook_url = models.URLField(max_length=500, blank=True)

    # Routing rules.
    min_severity = models.CharField(
        max_length=10, choices=Severity.choices, default=Severity.WARNING
    )
    environments = models.JSONField(
        default=list, blank=True, help_text="Server environments to include; empty = all."
    )
    include_servers = models.BooleanField(default=True)
    include_apps = models.BooleanField(default=True)
    notify_resolved = models.BooleanField(default=True)
    repeat_minutes = models.PositiveIntegerField(
        default=0,
        help_text="Re-send while an alert stays open and unacknowledged. 0 = never.",
    )

    last_delivery_at = models.DateTimeField(null=True, blank=True)
    last_delivery_ok = models.BooleanField(null=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(repeat_minutes__lte=1440), name="channel_repeat_at_most_a_day"
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def target_display(self) -> str:
        """Where it sends, without leaking the secret part."""
        if self.kind == ChannelKind.TELEGRAM:
            return f"chat {self.telegram_chat_id}" if self.telegram_chat_id else "no chat yet"
        from urllib.parse import urlsplit

        return urlsplit(self.webhook_url).hostname or "—"

    def matches(self, alert: Alert) -> bool:
        if SEVERITY_RANK.get(alert.severity, 0) < SEVERITY_RANK.get(self.min_severity, 0):
            return False
        if alert.server_id and not self.include_servers:
            return False
        if alert.monitor_id and not self.include_apps:
            return False
        if self.environments:
            server = alert.server if alert.server_id else getattr(alert.monitor, "server", None)
            # Apps not linked to a server have no environment; they always pass.
            if server is not None and server.environment not in self.environments:
                return False
        return True


class DeliveryEvent(models.TextChoices):
    OPENED = "opened", "Opened"
    RESOLVED = "resolved", "Resolved"
    REPEAT = "repeat", "Reminder"
    TEST = "test", "Test"


class Delivery(models.Model):
    """One notification to one channel; also the outbox's memory of what was sent."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="deliveries")
    alert = models.ForeignKey(
        Alert, null=True, blank=True, on_delete=models.CASCADE, related_name="deliveries"
    )
    event = models.CharField(max_length=10, choices=DeliveryEvent.choices)
    ok = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Opened/resolved go out at most once per channel, even with two relay processes.
            models.UniqueConstraint(
                fields=["alert", "channel", "event"],
                condition=Q(event__in=["opened", "resolved"]),
                name="delivery_once_per_alert_channel_event",
            ),
        ]
        indexes = [models.Index(fields=["channel", "-created_at"], name="delivery_channel_recent")]

    def __str__(self):
        return f"{self.channel} {self.event} {'ok' if self.ok else 'failed'}"


class MaintenanceWindow(models.Model):
    """While active, alerts on covered servers/apps are still recorded but nobody is
    notified. Muting a server also mutes the applications linked to it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    title = models.CharField(max_length=120)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    all_targets = models.BooleanField(
        default=False, help_text="Mute everything: every server and application."
    )
    servers = models.ManyToManyField("fleet.Server", blank=True, related_name="maintenance")
    monitors = models.ManyToManyField("monitors.Monitor", blank=True, related_name="maintenance")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="maintenance_ends_after_start"
            ),
        ]
        indexes = [models.Index(fields=["ends_at", "starts_at"], name="maintenance_time")]

    def __str__(self):
        return self.title

    @property
    def state(self) -> str:
        from django.utils import timezone

        now = timezone.now()
        return "upcoming" if now < self.starts_at else "active" if now < self.ends_at else "ended"
