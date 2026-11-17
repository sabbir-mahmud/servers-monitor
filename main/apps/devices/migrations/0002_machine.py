"""One table for everything an account watches.

Machine holds what servers and devices share (account, name, hostname, last seen).
Device becomes its multi-table child: its rows keep their ids, which become the link to
their Machine row, so DeviceLocation rows and every URL stay valid. fleet 0010 does the
same for servers.
"""

import uuid

import django.db.models.deletion
import django.db.models.functions.text
from django.conf import settings
from django.db import migrations, models

from apps.core.migration_ops import SetModelBases


def copy_devices(apps, schema_editor):
    Machine = apps.get_model("devices", "Machine")
    Device = apps.get_model("devices", "Device")
    for d in Device.objects.all().iterator():
        Machine.objects.create(
            id=d.id,
            account_id=d.account_id,
            kind="device",
            name=d.name,
            hostname=d.hostname,
            last_seen_at=d.last_seen_at,
            created_by_id=d.created_by_id,
        )
        # auto_now_add would stamp "now" on create.
        Machine.objects.filter(pk=d.id).update(created_at=d.created_at)


class Migration(migrations.Migration):
    dependencies = [
        ("devices", "0001_initial"),
        ("tenants", "0002_memberships"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Machine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid7, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("server", "Linux server"), ("device", "Computer or phone")], editable=False, max_length=10)),
                ("name", models.CharField(max_length=100)),
                ("hostname", models.CharField(blank=True, max_length=255)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="machines", to="tenants.account")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["name"],
                "indexes": [models.Index(fields=["account", "kind", "name"], name="machine_account_kind")],
                "constraints": [
                    models.UniqueConstraint(
                        models.F("account"),
                        django.db.models.functions.text.Lower("name"),
                        name="machine_name_unique_per_account",
                        violation_error_message="Another device or server in this account has that name.",
                    )
                ],
            },
        ),
        migrations.RunPython(copy_devices, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="device", name="device_name_unique_per_account"),
        migrations.RemoveIndex(model_name="device", name="device_account_person"),
        migrations.RemoveField(model_name="device", name="account"),
        migrations.RemoveField(model_name="device", name="name"),
        migrations.RemoveField(model_name="device", name="hostname"),
        migrations.RemoveField(model_name="device", name="last_seen_at"),
        migrations.RemoveField(model_name="device", name="created_by"),
        migrations.RemoveField(model_name="device", name="created_at"),
        # The id column stays as it is; only Django's view of it changes, from a plain
        # UUID primary key to the link to Machine (same column, same values).
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="device", name="id"),
                migrations.AddField(
                    model_name="device",
                    name="machine_ptr",
                    field=models.OneToOneField(
                        db_column="id", db_constraint=False, on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True, primary_key=True, related_name="device", serialize=False, to="devices.machine",
                    ),
                    preserve_default=False,
                ),
            ],
        ),
        # Now add the real foreign key from device.id to machine.id.
        migrations.AlterField(
            model_name="device",
            name="machine_ptr",
            field=models.OneToOneField(
                db_column="id", on_delete=django.db.models.deletion.CASCADE, parent_link=True,
                primary_key=True, related_name="device", serialize=False, to="devices.machine",
            ),
        ),
        SetModelBases("device", ("devices.machine",)),
    ]
