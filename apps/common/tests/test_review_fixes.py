"""Regressions for the review findings in apps/common and the migrations."""

import pytest
from django.core.cache import caches
from django.http import Http404

from apps.common.shortcuts import get_scoped_object_or_404
from apps.credentials.models import WorkspaceCredentialOverride


class TestTheNonEnforcingTwinIsGone:
    """Two classes named WorkspaceScopedManager — one of which enforces nothing —
    is a trap: an import from the wrong module silently loses tenant isolation."""

    def test_managers_no_longer_exports_one(self):
        from apps.common import managers

        assert not hasattr(managers, "WorkspaceScopedManager")

    def test_scoping_is_the_only_definition(self):
        from apps.common import scoping

        assert hasattr(scoping, "WorkspaceScopedManager")

    def test_the_org_scoped_manager_is_still_there(self):
        from apps.common.managers import OrgScopedManager

        assert hasattr(OrgScopedManager, "for_org")


@pytest.mark.django_db
class TestScopedShortcut:
    def test_a_missing_workspace_is_a_programming_error_not_a_404(self, tenancy):
        """for_workspace(None) raises on purpose — a filter that would match
        nothing. Turning it into a 404 here would hide the bug it exists to
        surface."""
        with pytest.raises(ValueError, match="needs a workspace"):
            get_scoped_object_or_404(WorkspaceCredentialOverride, None, platform="instagram")

    def test_a_genuine_miss_is_still_a_404(self, tenancy):
        with pytest.raises(Http404):
            get_scoped_object_or_404(WorkspaceCredentialOverride, tenancy.workspace, platform="instagram")

    def test_a_malformed_pk_is_a_404(self, tenancy):
        with pytest.raises(Http404):
            get_scoped_object_or_404(WorkspaceCredentialOverride, tenancy.workspace, pk="not-a-uuid")

    def test_a_hit_is_returned(self, tenancy):
        override = WorkspaceCredentialOverride.objects.create(
            workspace=tenancy.workspace, platform="instagram", credentials={"client_id": "a"}
        )

        assert get_scoped_object_or_404(WorkspaceCredentialOverride, tenancy.workspace, pk=override.pk) == override

    def test_another_workspaces_object_is_a_404(self, tenancy, other_tenancy):
        override = WorkspaceCredentialOverride.objects.create(
            workspace=other_tenancy.workspace, platform="instagram", credentials={"client_id": "a"}
        )

        with pytest.raises(Http404):
            get_scoped_object_or_404(WorkspaceCredentialOverride, tenancy.workspace, pk=override.pk)


@pytest.mark.django_db
class TestCacheTableMatchesTheConfiguration:
    """The migration used to hardcode `cache_table`, so any deployment that set
    CACHE_URL to another dbcache name got an unusable cache — and a 500 on every
    login POST, since the rate limiter is the first thing to touch it."""

    def test_the_configured_table_exists(self, settings):
        from django.db import connection

        table = caches["default"]._table

        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [table])
            assert cursor.fetchone()[0] is not None

    def test_the_migration_names_no_table_literal(self):
        """createcachetable with no argument reads settings.CACHES itself."""
        import importlib
        import inspect

        module = importlib.import_module("apps.common.migrations.0001_cache_table")
        source = inspect.getsource(module)

        assert 'call_command("createcachetable", database=' in source

    def test_the_rate_limiter_can_actually_write(self):
        from django.core.cache import cache

        cache.set("review-probe", 1, 5)

        assert cache.get("review-probe") == 1
