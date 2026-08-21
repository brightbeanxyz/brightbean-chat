"""Capability warnings — ROADMAP contract 4's consumer side.

The acceptance criterion the issue names lives here: a button-heavy graph with
SMS-only capability data must produce **warnings, not errors**, and must still
publish.
"""

import pytest

from apps.common.platforms import Platform
from apps.flows.capabilities import BLOCK_TYPES, CAPABILITIES, capabilities_for, connected_platforms
from apps.flows.fixtures import button_heavy_graph, graph_for, node_fixture
from apps.flows.schema import empty_graph, validate_graph
from apps.flows.services import create_flow, publish, save_draft


class TestTheTable:
    def test_every_platform_has_capabilities(self):
        assert set(CAPABILITIES) == set(Platform.values)

    def test_an_unknown_platform_has_none(self):
        assert capabilities_for("carrier_pigeon") is None

    def test_supports_block_answers_only_about_block_types(self):
        """It used to resolve any graph-supplied string against the dataclass, so
        `max_text_len` and `inbound` both reported as supported "blocks"."""
        telegram = capabilities_for(Platform.TELEGRAM)

        assert telegram.supports_block("image") is True
        assert telegram.supports_block("max_text_len") is False
        assert telegram.supports_block("inbound") is False
        assert telegram.supports_block("window_hours") is False

    def test_the_block_type_set_matches_the_schema(self):
        """Two lists of block types is one too many; this is what stops them
        drifting apart."""
        from apps.flows.schema.nodes import SHARED_DEFS

        assert set(SHARED_DEFS["message_block"]["discriminator"]["mapping"]) == BLOCK_TYPES

    def test_sms_has_no_buttons_and_email_takes_nothing_inbound(self):
        assert capabilities_for(Platform.SMS).buttons is False
        assert capabilities_for(Platform.EMAIL).inbound is False


class TestButtonHeavyOnSms:
    def test_it_warns_rather_than_erroring(self):
        result = validate_graph(button_heavy_graph(), platforms=[Platform.SMS])

        assert result.errors == []
        assert result.is_publishable is True
        assert {issue.code for issue in result.warnings} == {"capability_unsupported"}

    def test_the_warnings_name_the_node_and_the_field(self):
        result = validate_graph(button_heavy_graph(), platforms=[Platform.SMS])

        assert {issue.node_id for issue in result.warnings} == {"ask"}
        assert "config.buttons" in {issue.path for issue in result.warnings}

    def test_the_same_graph_is_clean_on_telegram(self):
        result = validate_graph(button_heavy_graph(), platforms=[Platform.TELEGRAM])

        # Telegram has no native card block, so that one stands; the buttons and
        # quick replies it handles natively.
        assert {issue.path for issue in result.warnings} == {"config.blocks[1]"}

    def test_a_workspace_on_both_hears_about_the_sms_side_only(self):
        result = validate_graph(button_heavy_graph(), platforms=[Platform.TELEGRAM, Platform.SMS])
        by_platform = [issue.message for issue in result.warnings]

        assert any("sms does not support buttons" in message for message in by_platform)
        assert not any("telegram does not support buttons" in message for message in by_platform)

    @pytest.mark.django_db
    def test_publishing_it_succeeds(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Menu")
        save_draft(flow, button_heavy_graph(), user=tenancy.owner)

        assert publish(flow, user=tenancy.owner).version.published is True


class TestLimits:
    def test_too_many_buttons_for_the_platform_is_a_warning(self):
        result = validate_graph(button_heavy_graph(), platforms=[Platform.WHATSAPP])

        assert {issue.code for issue in result.warnings} >= {"capability_limit_exceeded"}
        assert result.errors == []

    def test_an_over_long_message_is_a_warning(self):
        graph = empty_graph()
        node = node_fixture("send_message", node_id="long")
        node["config"] = {"blocks": [{"type": "text", "text": "x" * 2000}]}
        graph["nodes"] = [node]

        result = validate_graph(graph, platforms=[Platform.SMS])

        assert [issue.code for issue in result.warnings] == ["capability_limit_exceeded"]


class TestMissingConnections:
    def test_an_sms_node_with_no_sms_channel_warns(self):
        result = validate_graph(graph_for("send_sms"), platforms=[Platform.TELEGRAM])

        assert [issue.code for issue in result.warnings] == ["no_connection_for_node"]

    def test_an_sms_node_with_an_sms_channel_does_not(self):
        assert validate_graph(graph_for("send_sms"), platforms=[Platform.SMS]).warnings == []

    def test_no_platforms_means_no_guessing(self):
        """The stub state until issue #4: with nothing to read, validation says
        nothing rather than warning about every channel at once."""
        assert validate_graph(graph_for("send_sms"), platforms=[]).warnings == []


@pytest.mark.django_db
class TestConnectedPlatforms:
    def test_it_is_empty_until_the_channels_app_lands(self, tenancy):
        """Documented stub (issue #4). The validator takes platforms as an
        argument precisely so the rules stay testable in the meantime."""
        assert connected_platforms(tenancy.workspace) == ()
