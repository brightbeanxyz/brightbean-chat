"""Model-level guarantees: scoping, and the two constraints that carry the invariants."""

import pytest
from django.db import IntegrityError, transaction

from apps.common.scoping import UnscopedQueryError
from apps.flows.models import Flow, FlowStatus, FlowVersion
from apps.flows.schema import empty_graph
from apps.flows.services import create_flow

pytestmark = pytest.mark.django_db


class TestScoping:
    def test_an_unscoped_query_refuses_to_run(self, tenancy):
        create_flow(workspace=tenancy.workspace, name="Welcome")

        with pytest.raises(UnscopedQueryError):
            list(Flow.objects.filter(name="Welcome"))
        with pytest.raises(UnscopedQueryError):
            list(FlowVersion.objects.all())

    def test_another_workspaces_flows_are_not_visible(self, tenancy, other_tenancy):
        create_flow(workspace=tenancy.workspace, name="Ours")
        create_flow(workspace=other_tenancy.workspace, name="Theirs")

        names = [flow.name for flow in Flow.objects.for_workspace(tenancy.workspace)]
        assert names == ["Ours"]

    def test_a_version_carries_its_own_workspace(self, tenancy):
        """SPEC §5 wants workspace_id on every tenant table, and it means
        FlowVersion can be fetched scoped without joining through the flow."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        version = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow)

        assert version.workspace_id == tenancy.workspace.pk


class TestConstraints:
    def test_a_version_number_cannot_repeat_within_a_flow(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        with pytest.raises(IntegrityError), transaction.atomic():
            FlowVersion(workspace=flow.workspace, flow=flow, version=1, graph_json=empty_graph()).save()

    def test_the_same_version_number_in_a_different_flow_is_fine(self, tenancy):
        first = create_flow(workspace=tenancy.workspace, name="One")
        second = create_flow(workspace=tenancy.workspace, name="Two")

        assert {v.version for v in FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=first)} == {1}
        assert {v.version for v in FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=second)} == {1}

    def test_a_flow_cannot_hold_two_published_versions(self, tenancy):
        """The partial unique index. Every other layer says "the published
        version" as if it is one row; this is what makes that true even if some
        future code path forgets the lock in services.publish()."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        first = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow)
        first.published = True
        first.save(update_fields=["published"])

        second = FlowVersion(workspace=flow.workspace, flow=flow, version=2, graph_json=empty_graph(), published=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            second.save()

    def test_many_unpublished_versions_are_allowed(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        for version in (2, 3, 4):
            FlowVersion(workspace=flow.workspace, flow=flow, version=version, graph_json=empty_graph()).save()

        assert FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=flow).count() == 4


class TestDefaults:
    def test_a_new_flow_is_a_draft_with_an_empty_first_version(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        version = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow)

        assert flow.status == FlowStatus.DRAFT
        assert version.version == 1
        assert version.published is False
        assert version.graph_json == empty_graph()

    def test_folder_is_an_empty_string_rather_than_null(self, tenancy):
        """One empty value, not two. A nullable folder would make every
        grouping query handle both '' and NULL or silently drop rows."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        assert flow.folder == ""
        assert Flow.objects.for_workspace(tenancy.workspace).filter(folder="").count() == 1


class TestRepresentations:
    def test_a_flow_is_named_by_its_name(self, tenancy):
        assert str(create_flow(workspace=tenancy.workspace, name="Welcome")) == "Welcome"

    def test_a_version_is_named_by_its_flow_and_number(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        version = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow)

        assert str(version) == f"{flow.pk} v1"
        assert version.is_draft is True
