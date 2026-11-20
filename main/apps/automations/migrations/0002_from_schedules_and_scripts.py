"""Schedules and scripts become automations, keeping their ids (so jobs a schedule started
can be relinked by id in operations 0010)."""

from django.db import migrations

SCHEDULE_FIELDS = ("strategy", "batch_size", "pause_seconds", "stop_on_failure", "is_enabled",
                   "next_run_at", "last_run_at", "created_by_id", "created_at")  # fmt: skip


def forwards(apps, schema_editor):
    Schedule = apps.get_model("operations", "Schedule")
    Script = apps.get_model("operations", "Script")
    Automation = apps.get_model("automations", "Automation")

    for s in Schedule.objects.all():
        a = Automation.objects.create(
            id=s.id, account_id=s.account_id, name=s.name, kind="action", action=s.action,
            args=s.args or {}, cron=s.cron, environments=s.environments or [],
            last_error=s.last_error, updated_by_id=s.created_by_id,
            **{f: getattr(s, f) for f in SCHEDULE_FIELDS},
        )  # fmt: skip
        a.servers.set(s.servers.all())
        a.groups.set(s.groups.all())
        a.health_gates.set(s.health_gates.all())

    for s in Script.objects.all():
        name = s.name
        if Automation.objects.filter(account_id=s.account_id, name=name).exists():
            name = f"{s.name[:91]} (script)"
        Automation.objects.create(
            id=s.id, account_id=s.account_id, name=name, description=s.description,
            kind="script", interpreter=s.interpreter, body=s.body,
            created_by_id=s.created_by_id, updated_by_id=s.updated_by_id,
            created_at=s.created_at,
        )  # fmt: skip


def backwards(apps, schema_editor):
    Schedule = apps.get_model("operations", "Schedule")
    Script = apps.get_model("operations", "Script")
    Automation = apps.get_model("automations", "Automation")

    for a in Automation.objects.filter(kind="action").exclude(cron=""):
        s = Schedule.objects.create(
            id=a.id, account_id=a.account_id, name=a.name, action=a.action, args=a.args,
            cron=a.cron, environments=a.environments, last_error=a.last_error[:300],
            **{f: getattr(a, f) for f in SCHEDULE_FIELDS},
        )  # fmt: skip
        s.servers.set(a.servers.all())
        s.groups.set(a.groups.all())
        s.health_gates.set(a.health_gates.all())
    for a in Automation.objects.filter(kind="script"):
        Script.objects.create(
            id=a.id, account_id=a.account_id, name=a.name, description=a.description,
            interpreter=a.interpreter, body=a.body, created_by_id=a.created_by_id,
            updated_by_id=a.updated_by_id,
        )  # fmt: skip
    Automation.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("automations", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
