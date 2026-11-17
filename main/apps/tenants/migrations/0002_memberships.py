from django.db import migrations

from apps.tenants import backfill


class Migration(migrations.Migration):
    """Existing installs: the old global roles become memberships of one Company account."""

    dependencies = [
        ("tenants", "0001_initial"),
        ("users", "0004_sso"),
    ]

    operations = [
        migrations.RunPython(backfill.migrate_memberships, migrations.RunPython.noop),
    ]
