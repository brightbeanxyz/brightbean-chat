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


#: Django app labels this project owns. Third-party models (auth, sessions,
#: allauth, contenttypes) are somebody else's design and not this sweep's
#: business.
FIRST_PARTY_APPS = frozenset(
    {
        "accounts",
        "api",
        "broadcasts",
        "campaigns",
        "channels",
        "common",
        "contacts",
        "credentials",
        "flows",
        "inbox",
        "media_library",
        "members",
        "messaging",
        "notifications",
        "organizations",
        "queueing",
        "workspaces",
    }
)

#: First-party models that deliberately hold no tenant data, and why. Every
#: entry is a reviewed line, the way ``tests/idor.py``'s ``WAIVED_ROUTES`` is:
#: the mechanism is that a *new* model cannot be added without landing in one
#: list or the other.
NOT_TENANT_DATA: dict[str, str] = {
    "accounts.User": "A person, not a tenant's row. Membership is what scopes them.",
    "organizations.Organization": "The tenant above workspaces; scoping it by workspace is circular.",
    "workspaces.Workspace": "The tenant itself.",
    "members.OrgMembership": "Defines who belongs to an organization — the input to scoping.",
    "members.WorkspaceMembership": "Defines who belongs to a workspace, likewise.",
    "members.Invitation": "Organization-level (SPEC §4.1), and read by token before any workspace is known.",
    "credentials.PlatformCredential": (
        "The org-level half of the credential-resolution chain: one row can serve every workspace "
        "in the organization, which is the point of it."
    ),
    "channels.WebhookEventLog": (
        "Hangs off the connection. The inbound webhook path has no session and no workspace — the "
        "caller is a platform — so there is nothing to scope it by at write time."
    ),
    "messaging.SendBucket": "Rate-limit state per connection. Not tenant data; not readable as any.",
    "common.RateLimitCounter": "Deployment-wide counters keyed by a hashed identity.",
    "notifications.Notification": (
        "Addressed to a user, and the bell reads across every workspace they belong to. The "
        "workspace is denormalised into ``payload`` rather than being a column, which is what "
        "issue #29's erasure had to query."
    ),
    "notifications.NotificationDelivery": "Cascades from the notification; one delivery attempt.",
    "notifications.NotificationSetting": "One row per user per event type.",
}


class TestTheInvariantIsChecked:
    def test_a_system_check_guards_the_manager_ordering(self):
        """apps.common.checks.check_workspace_scoped_models, since the property
        is declaration order and nothing in the syntax protects it."""
        from apps.common.checks import check_workspace_scoped_models

        assert check_workspace_scoped_models() == []

    def test_every_tenant_model_inherits_the_base(self):
        """The sweep SECURITY-BASELINE §1 asks for, in both directions.

        This used to build the list of scoped models and then assert only that
        the *test* model was in it — which cannot fail for anything real, and so
        proved nothing about the project. Issue #29's traceability pass is
        exactly where that surfaces: a baseline item whose linked test cannot go
        red is an unenforced item with a green tick beside it.

        What matters is the other direction. A model holding tenant data that
        does **not** inherit the base gets Django's plain manager, so
        ``Model.objects.filter(...)`` runs happily across every workspace and
        nothing raises. So: every first-party model is either scoped or listed
        below with the reason it is not, and a new model is a decision somebody
        has to make rather than a default they inherit.
        """
        from django.apps import apps as django_apps

        ours = {model._meta.label for model in django_apps.get_models() if model._meta.app_label in FIRST_PARTY_APPS}
        scoped = {model._meta.label for model in django_apps.get_models() if issubclass(model, WorkspaceScopedModel)}

        assert ours - scoped == set(NOT_TENANT_DATA), (
            "A model is neither workspace-scoped nor recorded as non-tenant data. Inherit "
            "apps.common.scoping.WorkspaceScopedModel, or add it to NOT_TENANT_DATA with the "
            "reason it holds nothing a tenant owns (SECURITY-BASELINE §1)."
        )

    def test_no_waiver_holds_a_workspace_column(self):
        """The interesting contradiction: a model claiming to be non-tenant data
        while carrying a ``workspace`` foreign key is one or the other, and the
        combination is how a real tenant table ends up with a plain manager."""
        from django.apps import apps as django_apps

        # ``WorkspaceMembership`` is the one legitimate case: it carries the
        # column because a membership *is* a (user, workspace) pair, and scoping
        # it by workspace would ask the table that answers "may you see this
        # workspace" to first know which workspace you may see.
        circular = {"members.WorkspaceMembership"}

        contradictions = []
        for label in set(NOT_TENANT_DATA) - circular:
            model = django_apps.get_model(label)
            if any(field.name == "workspace" for field in model._meta.get_fields()):
                contradictions.append(label)

        assert not contradictions, (
            f"These are recorded as non-tenant data but carry a workspace column: {contradictions}"
        )
