"""Jobs point at the automation they're a run of; Schedule and Script live on as
automations (copied, ids kept, by automations 0002)."""

import django.db.models.deletion
from django.db import migrations, models


def relink(apps, schema_editor):
    Job = apps.get_model("operations", "Job")
    Automation = apps.get_model("automations", "Automation")
    known = set(Automation.objects.values_list("id", flat=True))
    for job in Job.objects.exclude(schedule_id=None).only("id", "schedule_id"):
        if job.schedule_id in known:
            Job.objects.filter(pk=job.pk).update(automation_id=job.schedule_id)


def unlink(apps, schema_editor):
    Job = apps.get_model("operations", "Job")
    Schedule = apps.get_model("operations", "Schedule")
    known = set(Schedule.objects.values_list("id", flat=True))
    for job in Job.objects.exclude(automation_id=None).only("id", "automation_id"):
        if job.automation_id in known:
            Job.objects.filter(pk=job.pk).update(schedule_id=job.automation_id)


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0009_scripts"),
        ("automations", "0002_from_schedules_and_scripts"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="automation",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="jobs", to="automations.automation",
            ),
        ),
        migrations.RunPython(relink, unlink),
        migrations.RemoveField(model_name="job", name="schedule"),
        migrations.DeleteModel(name="Schedule"),
        migrations.DeleteModel(name="Script"),
    ]
