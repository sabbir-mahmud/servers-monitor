"""Git deploys: a project (repo + branch) deployed to servers on push or on demand.

The steps run by `smctl deploy-run` on each server, as the app's user, after checking out
the commit. The repo, branch and folder are fixed on the server (`smctl app add`); the
dashboard chooses the commit and, for apps that allow it, the deploy steps."""

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


class DeployMode(models.TextChoices):
    AUTO = "auto", "Deploy every push automatically"
    APPROVAL = "approval", "Wait for an Admin to approve each push"


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    account = models.ForeignKey("tenants.Account", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=100)
    app = models.CharField(
        max_length=64, validators=[APP_NAME], help_text="Its name on the servers (smctl app add)."
    )
    repo_url = models.CharField("Repository", max_length=300)
    branch = models.CharField(max_length=200, default="main", validators=[BRANCH])
    servers = models.ManyToManyField("fleet.Server", blank=True, related_name="deploy_projects")
    mode = models.CharField(max_length=10, choices=DeployMode.choices, default=DeployMode.AUTO)
    steps = models.TextField(
        blank=True,
        help_text="Run in the checkout as the app's user, stopping at the first failing "
        "line. Leave empty to run the deploy script kept on each server.",
    )
    strategy = models.CharField(max_length=8, choices=Strategy.choices, default=Strategy.ALL)
    batch_size = models.PositiveSmallIntegerField(default=1)
    stop_on_failure = models.BooleanField(default=True)
    health_gates = models.ManyToManyField("monitors.Monitor", blank=True, related_name="+")
    webhook_secret = models.TextField(editable=False)  # encrypted (apps/core/crypto)
    is_enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["account", "name"], name="deploy_project_name"),
        ]

    def __str__(self):
        return self.name

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


class DeployStatus(models.TextChoices):
    WAITING = "waiting", "Needs approval"
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Deploying"
    SUCCEEDED = "succeeded", "Deployed"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"
    SUPERSEDED = "superseded", "Skipped"


OPEN_STATUSES = (DeployStatus.WAITING, DeployStatus.QUEUED, DeployStatus.RUNNING)


class Source(models.TextChoices):
    PUSH = "push", "Push"
    MANUAL = "manual", "Deploy now"
    ROLLBACK = "rollback", "Redeploy"


class Deployment(models.Model):
    """One commit going out to a project's servers. Its job holds each server's log."""

    account_path = "project__account"

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="deployments")
    sha = models.CharField(max_length=40, blank=True)  # "" = the branch's latest
    message = models.CharField(max_length=300, blank=True)
    author = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices)
    status = models.CharField(max_length=12, choices=DeployStatus.choices)
    error = models.CharField(max_length=500, blank=True)
    steps = models.TextField(blank=True)  # what was sent, as it was then
    job = models.ForeignKey(
        "operations.Job", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    delivery_id = models.CharField(max_length=100, blank=True)  # webhook replay guard
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
                fields=["project", "delivery_id"], condition=~Q(delivery_id=""),
                name="deployment_delivery_once",
            ),
        ]  # fmt: skip
        indexes = [
            models.Index(fields=["status"], condition=Q(status__in=["queued", "running"]),
                         name="deployment_open"),
        ]  # fmt: skip
        permissions = [("approve_deployment", "Can approve or reject deploys")]

    def __str__(self):
        return f"{self.project} {self.short_sha}"

    @property
    def short_sha(self) -> str:
        return self.sha[:7] if self.sha else "latest"

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES
