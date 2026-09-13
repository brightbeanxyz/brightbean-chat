"""A flow that cannot run says so.

The gap these cover shipped and was found by a person, not a test: importing a
template into a workspace with no Instagram account produced a flow that
validated clean, published, showed "Live", and was inert. Every screen was
telling the truth about the graph and none was telling the truth about whether
anything would ever reach it.

Warnings, never errors. Publishing before connecting the channel is a
legitimate order of work — SPEC §9.1 makes channel findings non-blocking — so
these must not turn a normal sequence into a refusal.
"""

from typing import Any

import pytest

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.flows import services
from apps.flows.models import Trigger
from apps.flows.services import create_flow
from apps.flows.tests.support import graph, node
from apps.flows.triggers.types import TriggerType

#: A graph node that is valid and does nothing, so publish has something to accept.
NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def _codes(flow: Any) -> set[str]:
    version = services.latest_version(flow)
    assert version is not None
    result = services.validate_for_workspace(version.graph_json, flow.workspace, flow=flow)
    return {issue["code"] for issue in result.as_dict()["warnings"]}


def _instagram(workspace: Any) -> ChannelConnection:
    return ChannelConnection.objects.create(
        workspace=workspace,
        platform=Platform.INSTAGRAM,
        external_id=f"ig-{workspace.pk}",
        display_name="Test account",
        status=ConnectionStatus.ACTIVE,
    )


@pytest.mark.django_db
class TestNothingStartsIt:
    def test_a_flow_with_no_trigger_says_it_will_not_run(self, tenancy: Any) -> None:
        flow = create_flow(workspace=tenancy.workspace, name="Orphan")

        assert "flow_has_no_trigger" in _codes(flow)

    def test_a_flow_whose_triggers_are_all_off_says_so(self, tenancy: Any) -> None:
        """How every imported template arrives — triggers switched off by design."""
        flow = create_flow(workspace=tenancy.workspace, name="Imported")
        Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.COMMENT, config_json={}, enabled=False
        )

        codes = _codes(flow)
        assert "flow_triggers_all_disabled" in codes
        assert "flow_has_no_trigger" not in codes

    def test_one_enabled_trigger_is_enough(self, tenancy: Any) -> None:
        flow = create_flow(workspace=tenancy.workspace, name="Half on")
        _instagram(tenancy.workspace)
        for enabled in (False, True):
            Trigger.objects.create(
                workspace=tenancy.workspace, flow=flow, type=TriggerType.COMMENT, config_json={}, enabled=enabled
            )

        assert _codes(flow) == set()


@pytest.mark.django_db
class TestTheChannelIsNotConnected:
    def test_it_names_the_platform_nothing_can_arrive_on(self, tenancy: Any) -> None:
        flow = create_flow(workspace=tenancy.workspace, name="Instagram only")
        Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.STORY_REPLY, config_json={}, enabled=True
        )

        assert "flow_platform_not_connected" in _codes(flow)

    def test_connecting_the_channel_clears_it(self, tenancy: Any) -> None:
        flow = create_flow(workspace=tenancy.workspace, name="Instagram only")
        Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.STORY_REPLY, config_json={}, enabled=True
        )
        _instagram(tenancy.workspace)

        assert "flow_platform_not_connected" not in _codes(flow)

    def test_a_channel_independent_trigger_never_raises_it(self, tenancy: Any) -> None:
        """``api`` resolves a channel from the contact, so it names no platform."""
        flow = create_flow(workspace=tenancy.workspace, name="Called from outside")
        Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.API, config_json={}, enabled=True
        )

        assert "flow_platform_not_connected" not in _codes(flow)


@pytest.mark.django_db
class TestTheyNeverBlockAPublish:
    def test_a_flow_nothing_can_reach_still_publishes(self, tenancy: Any) -> None:
        """Publish first, connect the channel second, is a normal order of work."""
        flow = create_flow(workspace=tenancy.workspace, name="Not yet reachable")
        services.save_draft(flow, graph([node("a", "action", NOOP_ACTION)]))
        Trigger.objects.create(
            workspace=tenancy.workspace, flow=flow, type=TriggerType.STORY_REPLY, config_json={}, enabled=True
        )

        published = services.publish(flow)

        assert published.version.published is True
        assert published.validation.errors == []
        assert "flow_platform_not_connected" in {issue.code for issue in published.validation.warnings}
