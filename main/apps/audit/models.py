from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only record of who did what. Rows outlive the user and server they mention."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    # Kept as text so the trail still reads correctly after the user is deleted.
    actor_label = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=200, blank=True)
    server = models.ForeignKey(
        "fleet.Server", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    data = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "-created_at"])]

    def __str__(self):
        return f"{self.actor_label or 'system'} {self.action} {self.target}"
