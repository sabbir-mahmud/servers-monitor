"""Servers join the shared Machine table (see devices 0002). Their ids are kept, so
commands, samples, rollups, alerts, maintenance windows, job targets, monitors and
audit entries pointing at a server stay valid with no change."""

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Lower

from apps.core.migration_ops import SetModelBases


def copy_servers(apps, schema_editor):
    Machine = apps.get_model("devices", "Machine")
    Server = apps.get_model("fleet", "Server")
    for s in Server.objects.all().iterator():
        # Names are now unique per account across servers and devices (case-insensitive).
        name, n = s.name, 2
        while Machine.objects.filter(account_id=s.account_id).annotate(l=Lower("name")).filter(l=name.lower()).exists():
            name, n = f"{s.name} ({n})", n + 1
        Machine.objects.create(
            id=s.id,
            account_id=s.account_id,
            kind="server",
            name=name,
            hostname=s.hostname,
            last_seen_at=s.last_seen_at,
            created_by_id=s.created_by_id,
        )
        Machine.objects.filter(pk=s.id).update(created_at=s.created_at)


class Migration(migrations.Migration):
    dependencies = [
        ("fleet", "0009_account_required"),
        ("devices", "0002_machine"),
    ]

    operations = [
        migrations.RunPython(copy_servers, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="server", name="server_name_per_account"),
        migrations.RemoveIndex(model_name="server", name="fleet_serve_account_609fe7_idx"),
        migrations.RemoveField(model_name="server", name="account"),
        migrations.RemoveField(model_name="server", name="name"),
        migrations.RemoveField(model_name="server", name="hostname"),
        migrations.RemoveField(model_name="server", name="last_seen_at"),
        migrations.RemoveField(model_name="server", name="created_by"),
        migrations.RemoveField(model_name="server", name="created_at"),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="server", name="id"),
                migrations.AddField(
                    model_name="server",
                    name="machine_ptr",
                    field=models.OneToOneField(
                        db_column="id", db_constraint=False, on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True, primary_key=True, related_name="server", serialize=False, to="devices.machine",
                    ),
                    preserve_default=False,
                ),
            ],
        ),
        migrations.AlterField(
            model_name="server",
            name="machine_ptr",
            field=models.OneToOneField(
                db_column="id", on_delete=django.db.models.deletion.CASCADE, parent_link=True,
                primary_key=True, related_name="server", serialize=False, to="devices.machine",
            ),
        ),
        SetModelBases("server", ("devices.machine",)),
    ]
