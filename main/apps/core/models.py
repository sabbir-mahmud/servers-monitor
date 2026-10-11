from django.db import models


class SystemState(models.Model):
    """Small key/value store for the dashboard's own health: watchdog pings, backups,
    downtime, relay usage. Written by the relay process, read by the System page."""

    key = models.CharField(max_length=64, primary_key=True)
    value = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key: str, default=None):
        row = cls.objects.filter(key=key).first()
        return row.value if row else default

    @classmethod
    def put(cls, key: str, value) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": value})


class RelayUsage(models.Model):
    """Redis commands per day: the dashboard's own (exact) and agents' (estimated)."""

    day = models.DateField(primary_key=True)
    dashboard = models.BigIntegerField(default=0)
    agent = models.BigIntegerField(default=0)

    def __str__(self):
        return f"{self.day}: {self.dashboard + self.agent}"
