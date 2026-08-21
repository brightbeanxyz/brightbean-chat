"""Provision the database cache table.

``CACHE_URL`` defaults to ``dbcache://cache_table``: LocMemCache is per-process
and both the Dockerfile and the Procfile run gunicorn with two workers, so a
per-IP counter kept there is evaded by landing on the other worker — which is
exactly what the auth rate limiter must not allow (SECURITY-BASELINE §8).
Postgres is the rate limiter (SPEC §22, no Redis).

``manage.py createcachetable`` would be a second, forgettable deploy step; every
path that already runs migrations (``docker compose up``, the Procfile release
phase, pytest's test-database setup) gets the table from here instead.
"""

from typing import Any

from django.core.management import call_command
from django.db import migrations

CACHE_TABLE = "cache_table"


def create_cache_table(apps: Any, schema_editor: Any) -> None:
    # call_command rather than hand-written DDL: the table's shape is Django's
    # to define, and it has changed before (the cache_key column length).
    call_command("createcachetable", CACHE_TABLE, database=schema_editor.connection.alias, verbosity=0)


def drop_cache_table(apps: Any, schema_editor: Any) -> None:
    schema_editor.execute(f'DROP TABLE IF EXISTS "{CACHE_TABLE}"')


class Migration(migrations.Migration):
    initial = True

    dependencies = []  # noqa: RUF012

    operations = [
        migrations.RunPython(create_cache_table, drop_cache_table),
    ]
