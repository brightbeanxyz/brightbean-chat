"""The enforcing workspace-scoped manager (SECURITY-BASELINE §1, deviation 2)."""

import pytest
from django.db import models

from apps.common.scoping import UnscopedQueryError, WorkspaceScopedManager, WorkspaceScopedModel
from apps.credentials.models import WorkspaceCredentialOverride
from tests.support import create_tenancy

MODEL = WorkspaceCredentialOverride


@pytest.fixture
def two_workspaces(db):
    mine = create_tenancy("mine")
    theirs = create_tenancy("theirs")
    MODEL.objects.create(workspace=mine.workspace, platform="instagram", credentials={"client_id": "a"})
    MODEL.objects.create(workspace=theirs.workspace, platform="instagram", credentials={"client_id": "b"})
    return mine, theirs


@pytest.mark.django_db
class TestUnscopedAccessRaises:
    """Studio's manager only *offers* .for_workspace(); this one insists on it."""

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param(lambda qs: list(qs), id="iteration"),
            pytest.param(lambda qs: qs.count(), id="count"),
            pytest.param(lambda qs: qs.exists(), id="exists"),
            pytest.param(lambda qs: qs.first(), id="first"),
            pytest.param(lambda qs: list(qs.iterator()), id="iterator"),
            pytest.param(lambda qs: qs.aggregate(n=models.Count("id")), id="aggregate"),
            pytest.param(lambda qs: qs.update(platform="whatsapp"), id="update"),
            pytest.param(lambda qs: qs.delete(), id="delete"),
            pytest.param(lambda qs: qs.in_bulk(), id="in_bulk"),
        ],
    )
    def test_every_terminal_operation_is_guarded(self, two_workspaces, operation):
        with pytest.raises(UnscopedQueryError):
            operation(MODEL.objects.all())

    def test_the_guard_survives_chaining(self, two_workspaces):
        """.filter() must not look like scoping."""
        with pytest.raises(UnscopedQueryError):
            list(MODEL.objects.filter(platform="instagram").order_by("platform"))

    def test_get_is_guarded_too(self, two_workspaces):
        with pytest.raises(UnscopedQueryError):
            MODEL.objects.get(platform="instagram")

    def test_the_error_names_the_way_out(self, two_workspaces):
        with pytest.raises(UnscopedQueryError) as caught:
            MODEL.objects.count()

        message = str(caught.value)
        assert "for_workspace" in message
        assert "unscoped" in message


@pytest.mark.django_db
class TestScopedAccess:
    def test_for_workspace_returns_only_that_workspace(self, two_workspaces):
        mine, _ = two_workspaces

        rows = list(MODEL.objects.for_workspace(mine.workspace))

        assert [row.credentials["client_id"] for row in rows] == ["a"]

    def test_for_workspace_accepts_an_id(self, two_workspaces):
        mine, _ = two_workspaces

        assert MODEL.objects.for_workspace(mine.workspace.pk).count() == 1

    def test_for_workspace_rejects_none(self, two_workspaces):
        """filter(workspace_id=None) would match nothing and look like an empty tenant."""
        with pytest.raises(ValueError, match="needs a workspace"):
            MODEL.objects.for_workspace(None)

    def test_unscoped_is_the_deliberate_escape_hatch(self, two_workspaces):
        assert MODEL.objects.unscoped().count() == 2

    def test_scope_survives_further_filtering(self, two_workspaces):
        mine, _ = two_workspaces

        assert MODEL.objects.for_workspace(mine.workspace).filter(platform="instagram").count() == 1


@pytest.mark.django_db
class TestDjangoInternalsStillWork:
    """The guard must not poison the paths Django itself drives."""

    def test_reverse_related_access_is_already_scoped(self, two_workspaces):
        mine, _ = two_workspaces

        # workspace.workspacecredentialoverrides is scoped by construction, so
        # it goes through the plain default manager and must not raise.
        assert mine.workspace.workspacecredentialoverrides.count() == 1

    def test_default_manager_is_the_plain_one(self):
        assert not isinstance(MODEL._meta.default_manager, WorkspaceScopedManager)

    def test_cascade_delete_works(self, two_workspaces):
        mine, _ = two_workspaces

        mine.workspace.delete()

        assert MODEL.objects.unscoped().count() == 1


class TestTheInvariantIsChecked:
    def test_a_system_check_guards_the_manager_ordering(self):
        """apps.common.checks.check_workspace_scoped_models, since the property
        is declaration order and nothing in the syntax protects it."""
        from apps.common.checks import check_workspace_scoped_models

        assert check_workspace_scoped_models() == []

    def test_every_tenant_model_inherits_the_base(self):
        from django.apps import apps as django_apps

        tenant_models = [m for m in django_apps.get_models() if issubclass(m, WorkspaceScopedModel)]

        assert MODEL in tenant_models
