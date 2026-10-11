import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class CommandStatus(models.TextChoices):
    PENDING = "pending", "Queued"
    RUNNING = "running", "Running"  # delivered to the agent
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    LOST = "lost", "Lost"  # delivered, but the agent never reported back in time
    EXPIRED = "expired", "Expired"  # never picked up within the delivery window
    CANCELLED = "cancelled", "Cancelled"
    WAITING = "waiting", "Waiting for its batch"  # rolling jobs: later batches


ACTIVE_STATUSES = (CommandStatus.PENDING, CommandStatus.RUNNING)


class Strategy(models.TextChoices):
    ALL = "all", "All at once"
    ROLLING = "rolling", "Rolling, in batches"


class JobState(models.TextChoices):
    RUNNING = "running", "Running"
    HALTED = "halted", "Halted"
    COMPLETED = "completed", "Completed"
    ABORTED = "aborted", "Aborted"


class Job(models.Model):
    """One operator request fanned out to N servers ("deploy api to all production")."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    action = models.CharField(max_length=64)
    args = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    schedule = models.ForeignKey(
        "Schedule", null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs"
    )

    # Rolling rollouts: batches of servers, each released when the previous one finished
    # (and the health-gate apps are up again).
    strategy = models.CharField(max_length=8, choices=Strategy.choices, default=Strategy.ALL)
    batch_size = models.PositiveSmallIntegerField(default=1)
    pause_seconds = models.PositiveIntegerField(default=0)
    stop_on_failure = models.BooleanField(default=True)
    health_gates = models.ManyToManyField("monitors.Monitor", blank=True, related_name="+")
    gate_timeout_seconds = models.PositiveIntegerField(default=600)
    state = models.CharField(max_length=10, choices=JobState.choices, default=JobState.RUNNING)
    current_batch = models.PositiveSmallIntegerField(default=0)
    batch_done_at = models.DateTimeField(null=True, blank=True)
    halt_reason = models.CharField(max_length=300, blank=True)
    accept_failures_through = models.SmallIntegerField(default=-1)  # set by Resume

    class Meta:
        ordering = ["-created_at"]
        permissions = [("run_shell", "Can run free-form shell commands on servers")]

    def __str__(self):
        return f"{self.action} ({self.created_at:%Y-%m-%d %H:%M})"

    @property
    def summary(self) -> str:
        if self.action == "shell":
            return self.args.get("command", "")[:80]
        if self.action == "_upgrade":
            return f"upgrade agent to {self.args.get('ref', '?')}"
        if self.action == "tail_follow":
            what = (
                f"journal of {self.args.get('name')}"
                if self.args.get("source") == "journal"
                else self.args.get("name")
            )
            grep = f" | {self.args['filter']}" if self.args.get("filter") else ""
            return f"follow {what}{grep}"
        if self.args.get("name"):
            return f"{self.action} {self.args['name']}"
        return self.action


class Command(models.Model):
    """One action on one server. Delivered at most once: the agent BLPOPs it from its
    mailbox, so only one consumer ever sees it, and a delivered-but-silent command becomes
    LOST rather than being re-sent: a deploy must never run twice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="commands")
    server = models.ForeignKey("fleet.Server", on_delete=models.CASCADE, related_name="commands")
    action = models.CharField(max_length=64)
    args = models.JSONField(default=dict, blank=True)
    timeout_seconds = models.PositiveIntegerField(default=300)
    batch = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(
        max_length=12, choices=CommandStatus.choices, default=CommandStatus.PENDING
    )
    exit_code = models.IntegerField(null=True, blank=True)
    output = models.TextField(blank=True)
    output_truncated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    deliver_by = models.DateTimeField()
    # When the command was pushed to the agent's relay mailbox. NULL = still in the outbox
    # (Redis was unreachable at queue time); the relay process retries it.
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["job", "server"], name="command_one_per_job_server"),
        ]
        indexes = [
            # Outbox and expiry: WHERE status = 'pending'. Partial, so it stays tiny no
            # matter how much history accumulates.
            models.Index(
                fields=["server", "created_at"],
                condition=Q(status="pending"),
                name="command_pending_by_server",
            ),
            # Sweep: everything still in flight.
            models.Index(
                fields=["status"],
                condition=Q(status__in=["pending", "running"]),
                name="command_active",
            ),
            models.Index(fields=["server", "-created_at"], name="command_server_recent"),
        ]

    def __str__(self):
        return f"{self.action} on {self.server_id} [{self.status}]"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class Schedule(models.Model):
    """Run an action on a cron schedule (e.g. renew certificates every night, deploy at 02:00)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True)
    action = models.CharField(max_length=64)
    args = models.JSONField(default=dict, blank=True)
    cron = models.CharField(
        max_length=100, help_text="minute hour day month weekday, e.g. 0 3 * * *"
    )
    servers = models.ManyToManyField("fleet.Server", blank=True, related_name="+")
    groups = models.ManyToManyField("fleet.ServerGroup", blank=True, related_name="+")
    environments = models.JSONField(default=list, blank=True)
    # Rollout options for each run (see Job).
    strategy = models.CharField(max_length=8, choices=Strategy.choices, default=Strategy.ALL)
    batch_size = models.PositiveSmallIntegerField(default=1)
    pause_seconds = models.PositiveIntegerField(default=0)
    stop_on_failure = models.BooleanField(default=True)
    health_gates = models.ManyToManyField("monitors.Monitor", blank=True, related_name="+")
    is_enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def targets(self):
        """Servers resolved now: chosen ones plus every member of the groups/environments."""
        from apps.fleet.models import Server

        q = Q(pk__in=self.servers.values("pk")) | Q(groups__in=self.groups.all())
        if self.environments:
            q |= Q(environment__in=self.environments)
        return Server.objects.filter(q, is_enabled=True).distinct().order_by("name")
