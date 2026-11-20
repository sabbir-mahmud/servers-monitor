"""Automations: something to run on servers, and how it starts.

One record covers what used to be four features. What it runs:

- an **action**: any agent action or server task (`restart_service`, `_task` upgrade, a
  custom action from agent.toml, a shell command);
- a **script** kept here, sent as a quoted shell command (`shell` action);
- a **git deploy** of a repo/branch the servers know as `app` (`_deploy` action).

How it starts: by hand (always), on a cron schedule, and/or (deploys) on a git push, each
optionally gated by an Admin's approval. Every start is a Run; each Run that goes ahead
is one Job with a Command per server, so output and rollouts work as for any job.
"""

import shlex
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q

from apps.operations.models import Strategy

APP_NAME = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
    "Letters, digits, '_', '-' and '.', as in `smctl app add NAME`.",
)
BRANCH = RegexValidator(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$", "Not a valid branch name.")


class Kind(models.TextChoices):
    ACTION = "action", "Action"
    SCRIPT = "script", "Script"
    DEPLOY = "deploy", "Git deploy"


class Interpreter(models.TextChoices):
    BASH = "bash", "Bash"
    SH = "sh", "POSIX sh"
    PYTHON = "python3", "Python 3"


class Automation(models.Model):
    MAX_BODY = 16000

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    account = models.ForeignKey("tenants.Account", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=8, choices=Kind.choices)

    # kind=action
    action = models.CharField(max_length=64, blank=True)
    args = models.JSONField(default=dict, blank=True)
    # kind=script
    interpreter = models.CharField(
        max_length=10, choices=Interpreter.choices, default=Interpreter.BASH
    )
    body = models.TextField(blank=True)
    # kind=deploy
    repo_url = models.CharField("Repository", max_length=300, blank=True)
    branch = models.CharField(max_length=200, blank=True, validators=[BRANCH])
    app = models.CharField(max_length=64, blank=True, validators=[APP_NAME])
    steps = models.TextField(blank=True)

    # Where: chosen servers plus every member of the groups/environments, resolved per run.
    servers = models.ManyToManyField("fleet.Server", blank=True, related_name="automations")
    groups = models.ManyToManyField("fleet.ServerGroup", blank=True, related_name="+")
    environments = models.JSONField(default=list, blank=True)

    # When: by hand always; plus a cron schedule and/or (deploys) every push.
    cron = models.CharField(max_length=100, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    on_push = models.BooleanField(default=False)
    webhook_secret = models.TextField(blank=True, editable=False)  # encrypted (apps/core/crypto)
    require_approval = models.BooleanField(default=False)

    # How: the rollout of each run (see operations.Job).
    strategy = models.CharField(max_length=8, choices=Strategy.choices, default=Strategy.ALL)
    batch_size = models.PositiveSmallIntegerField(default=1)
    pause_seconds = models.PositiveIntegerField(default=0)
    stop_on_failure = models.BooleanField(default=True)
    health_gates = models.ManyToManyField("monitors.Monitor", blank=True, related_name="+")

    is_enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["account", "name"], name="automation_name"),
        ]
        permissions = [("approve_run", "Can approve or reject runs")]

    def __str__(self):
        return self.name

    # ------------------------------------------------------------------ what it sends

    def script_command(self) -> str:
        """The shell command that runs the script (the agent runs it with /bin/sh -c).
        Quoted whole, so nothing in the body can break out of its interpreter."""
        body = self.body.replace("\r\n", "\n")
        if self.interpreter == Interpreter.SH:
            return body
        return f"exec {self.interpreter} -c {shlex.quote(body)} {shlex.quote(self.name)}"

    def job_spec(self, sha: str = "") -> tuple[str, dict]:
        """The (action, args) each run sends."""
        if self.kind == Kind.SCRIPT:
            return "shell", {"command": self.script_command(), "script": self.name}
        if self.kind == Kind.DEPLOY:
            return "_deploy", {"app": self.app, "sha": sha, "steps": self.steps}
        return self.action, dict(self.args)

    @property
    def needs_shell(self) -> bool:
        return self.kind == Kind.SCRIPT or (self.kind == Kind.ACTION and self.action == "shell")

    # ------------------------------------------------------------------ where

    def targets(self):
        """Servers resolved now: chosen ones plus every member of the groups/environments."""
        from apps.fleet.models import Server

        q = Q(pk__in=self.servers.values("pk")) | Q(groups__in=self.groups.all())
        if self.environments:
            q |= Q(environment__in=self.environments)
        return (
            Server.objects.filter(q, account_id=self.account_id, is_enabled=True)
            .distinct()
            .order_by("name")
        )

    @property
    def has_targets(self) -> bool:
        return bool(self.environments or self.servers.exists() or self.groups.exists())

    # ------------------------------------------------------------------ display

    @property
    def summary(self) -> str:
        """What it runs, in a few words."""
        if self.kind == Kind.SCRIPT:
            lines = len(self.body.splitlines())
            return f"{self.get_interpreter_display()} script, {lines} line{'s' * (lines != 1)}"
        if self.kind == Kind.DEPLOY:
            return f"{self.repo_label} · {self.branch}"
        from apps.operations.models import Job

        return Job(action=self.action, args=self.args).summary

    @property
    def line_count(self) -> int:
        return len(self.body.splitlines())

    @property
    def repo_label(self) -> str:
        """github.com/acme/api from any of the usual URL shapes."""
        url = self.repo_url.removesuffix(".git")
        if url.startswith("git@"):
            url = url[4:].replace(":", "/", 1)
        return url.split("://", 1)[-1].split("@", 1)[-1]

    def commit_url(self, sha: str) -> str:
        host = self.repo_label
        if not sha or "/" not in host:
            return ""
        if host.startswith("gitlab."):
            return f"https://{host}/-/commit/{sha}"
        if host.startswith("bitbucket.org/"):
            return f"https://{host}/commits/{sha}"
        return f"https://{host}/commit/{sha}"


class RunStatus(models.TextChoices):
    WAITING = "waiting", "Needs approval"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"
    SKIPPED = "skipped", "Skipped"


OPEN_STATUSES = (RunStatus.WAITING, RunStatus.QUEUED, RunStatus.RUNNING)


class Trigger(models.TextChoices):
    MANUAL = "manual", "By hand"
    SCHEDULE = "schedule", "Schedule"
    PUSH = "push", "Push"
    REDEPLOY = "redeploy", "Redeploy"


# Runs these triggers start are interchangeable: when several queue up, only the newest
# runs (five quick pushes, or schedule slots missed while a long run went on).
COALESCING = (Trigger.SCHEDULE, Trigger.PUSH)


class Run(models.Model):
    """One start of an automation. Its job holds each server's output."""

    account_path = "automation__account"

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="runs")
    trigger = models.CharField(max_length=10, choices=Trigger.choices)
    status = models.CharField(max_length=10, choices=RunStatus.choices)
    error = models.CharField(max_length=500, blank=True)
    # Chosen at Run now (else the automation's targets, resolved when it starts).
    servers = models.ManyToManyField("fleet.Server", blank=True, related_name="+")
    note = models.CharField(max_length=200, blank=True)
    # Git deploys: the commit ("" = the branch's latest) and what the push said about it.
    sha = models.CharField(max_length=40, blank=True)
    message = models.CharField(max_length=300, blank=True)
    author = models.CharField(max_length=100, blank=True)
    delivery_id = models.CharField(max_length=100, blank=True)  # webhook replay guard
    job = models.ForeignKey(
        "operations.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )  # fmt: skip
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )  # fmt: skip
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["automation", "delivery_id"], condition=~Q(delivery_id=""),
                name="run_delivery_once",
            ),
        ]  # fmt: skip
        indexes = [
            models.Index(fields=["status"], condition=Q(status__in=["waiting", "queued", "running"]),
                         name="run_open"),
        ]  # fmt: skip

    def __str__(self):
        return f"{self.automation} {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def short_sha(self) -> str:
        return self.sha[:7] if self.sha else "latest"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return None

    @property
    def title(self) -> str:
        if self.message:
            return self.message
        if self.automation.kind == Kind.DEPLOY:
            return f"Deploy {self.short_sha}"
        return self.automation.name
