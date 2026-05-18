"""Enable the pgvector extension on PostgreSQL.

Runs only on PostgreSQL — silently no-ops on SQLite so the test suite and
local lightweight development environments keep working.
"""

from __future__ import annotations

from django.db import migrations


def _enable_vector(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def _disable_vector(apps, schema_editor):
    # Intentional no-op on reverse: dropping the extension would also
    # destroy any other apps that use it.
    return


class Migration(migrations.Migration):

    initial = True
    dependencies: list = []

    operations = [
        migrations.RunPython(_enable_vector, _disable_vector),
    ]
