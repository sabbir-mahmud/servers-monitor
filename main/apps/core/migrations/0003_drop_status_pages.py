"""Status pages were removed as a feature. Drop their tables (if this database ever had
them), their permissions and the old migration records, so nothing is left orphaned.

Irreversible on purpose: the data is gone with the feature. Back up first if you need it.
"""

from django.db import migrations

TABLES = [  # children first, so foreign keys never block a drop
    "status_statusincidentupdate",
    "status_statusincident_affected",
    "status_statusincident",
    "status_statuspagemonitor",
    "status_statuspage",
]


def drop(apps, schema_editor):
    existing = set(schema_editor.connection.introspection.table_names())
    with schema_editor.connection.cursor() as cursor:
        for table in TABLES:
            if table in existing:
                cursor.execute(f"DROP TABLE {schema_editor.quote_name(table)}")
        cursor.execute("DELETE FROM django_migrations WHERE app = %s", ["status"])
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="status").delete()  # cascades to permissions


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0002_relay_usage"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(drop, migrations.RunPython.noop, elidable=True)]
