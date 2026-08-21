"""Provision the database cache table.

``CACHE_URL`` defaults to ``dbcache://cache_table``: LocMemCache is per-process
and both the Dockerfile and the Procfile run gunicorn with two workers, so a
per-IP counter kept there is evaded by landing on the other worker — which is
exactly what the auth rate limiter must not allow (SECURITY-BASELINE §8).
Postgres is the rate limiter (SPEC §22, no Redis).

``manage.py createcachetable`` would be a second, forgettable deploy step; every
path that already runs migrations (``docker compose up``, the Procfile release
phase, pytest's test-database setup) gets the table from here instead.

The table name is **not** hardcoded. ``createcachetable`` with no arguments reads
``settings.CACHES`` and creates the table each database-backed alias actually
names, so a deployment that sets ``CACHE_URL=dbcache://something_else`` gets
that table — and one that points ``CACHE_URL`` at local memory gets no table at
all, which is also correct.
"""

from typing import Any

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.db import BaseDatabaseCache
from django.core.management import call_command
from django.db import migrations


def _database_cache_tables() -> list[str]:
    """The tables the configured database caches expect to exist."""
    tables = []
    for alias in settings.CACHES:
        backend = caches[alias]
        if isinstance(backend, BaseDatabaseCache):
            # _table is the documented attribute Django's own createcachetable
            # reads; django-stubs just does not declare it.
            tables.append(backend._table)  # type: ignore[attr-defined]
    return tables


def create_cache_tables(apps: Any, schema_editor: Any) -> None:
    # call_command rather than hand-written DDL: the table's shape is Django's
    # to define, and it has changed before (the cache_key column length).
    call_command("createcachetable", database=schema_editor.connection.alias, verbosity=0)


def drop_cache_tables(apps: Any, schema_editor: Any) -> None:
    for table in _database_cache_tables():
        schema_editor.execute(f'DROP TABLE IF EXISTS "{table}"')


class Migration(migrations.Migration):
    initial = True

    dependencies = []  # noqa: RUF012

    operations = [
        migrations.RunPython(create_cache_tables, drop_cache_tables),
    ]
