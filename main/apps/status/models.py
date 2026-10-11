"""Public status pages, published as static files (no public endpoint on this dashboard).

The relay process renders each page to a self-contained index.html + status.json and
pushes them to GitHub (Pages, or Cloudflare Pages connected to that repo) when something
visible changed. Only what's listed here is ever published: display names, states, daily
uptime and incident updates. Never URLs, hosts, servers or error messages.
"""

import json
import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

from apps.core.fields import EncryptedTextField


class PublishKind(models.TextChoices):
    NONE = "none", "Don't publish (download it yourself)"
    GITHUB = "github", "GitHub repository (GitHub Pages / Cloudflare Pages)"


class StatusPage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)
    name = models.CharField(max_length=80, unique=True, help_text="Only shown in this dashboard.")
    title = models.CharField(max_length=120, help_text="Shown at the top of the public page.")
    description = models.CharField(max_length=300, blank=True)
    logo_url = models.URLField(max_length=300, blank=True, help_text="Optional https:// image.")
    homepage_url = models.URLField(
        max_length=300, blank=True, help_text="Optional link back to your site."
    )
    show_uptime = models.BooleanField(
        default=True, help_text="Show daily uptime bars and percentages."
    )
    monitors = models.ManyToManyField(
        "monitors.Monitor", through="StatusPageMonitor", related_name="+"
    )

    publish_kind = models.CharField(
        max_length=10, choices=PublishKind.choices, default=PublishKind.NONE
    )
    publish_config = EncryptedTextField(blank=True, default="")  # repo, branch, path, token, domain
    last_published_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_hash = models.CharField(max_length=64, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    needs_publish = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def config(self) -> dict:
        try:
            return json.loads(self.publish_config or "{}")
        except ValueError:
            return {}

    @property
    def public_url(self) -> str:
        cfg = self.config
        if cfg.get("domain"):
            return f"https://{cfg['domain']}/"
        if self.publish_kind == PublishKind.GITHUB and "/" in cfg.get("repo", ""):
            owner, repo = cfg["repo"].split("/", 1)
            path = cfg.get("path", "").strip("/")
            return f"https://{owner}.github.io/{repo}/" + (f"{path}/" if path else "")
        return ""


class StatusPageMonitor(models.Model):
    page = models.ForeignKey(StatusPage, on_delete=models.CASCADE, related_name="entries")
    monitor = models.ForeignKey("monitors.Monitor", on_delete=models.CASCADE, related_name="+")
    display_name = models.CharField(max_length=80)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "display_name"]
        constraints = [
            models.UniqueConstraint(fields=["page", "monitor"], name="status_entry_once")
        ]

    def __str__(self):
        return self.display_name


class IncidentStatus(models.TextChoices):
    INVESTIGATING = "investigating", "Investigating"
    IDENTIFIED = "identified", "Identified"
    MONITORING = "monitoring", "Monitoring"
    RESOLVED = "resolved", "Resolved"


class Impact(models.TextChoices):
    MINOR = "minor", "Minor"
    MAJOR = "major", "Major"
    MAINTENANCE = "maintenance", "Maintenance"


class StatusIncident(models.Model):
    page = models.ForeignKey(StatusPage, on_delete=models.CASCADE, related_name="incidents")
    title = models.CharField(max_length=160)
    impact = models.CharField(max_length=12, choices=Impact.choices, default=Impact.MINOR)
    status = models.CharField(
        max_length=14, choices=IncidentStatus.choices, default=IncidentStatus.INVESTIGATING
    )
    affected = models.ManyToManyField(StatusPageMonitor, blank=True, related_name="incidents")
    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def is_active(self) -> bool:
        return self.resolved_at is None


class StatusIncidentUpdate(models.Model):
    incident = models.ForeignKey(StatusIncident, on_delete=models.CASCADE, related_name="updates")
    status = models.CharField(max_length=14, choices=IncidentStatus.choices)
    message = models.TextField(max_length=2000)
    created_at = models.DateTimeField(default=timezone.now)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.incident}: {self.get_status_display()}"


repo_validator = RegexValidator(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", "Use owner/repository.")
domain_validator = RegexValidator(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    "Enter a domain like status.example.com.",
)
