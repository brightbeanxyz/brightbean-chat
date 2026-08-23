"""The builder payload's `triggers` key, and warnings recomputed from bindings."""

import pytest
from django.urls import reverse

from apps.common.platforms import Platform
from apps.flows.models import Trigger, TriggerType
from apps.flows.services import validate_for_workspace
from apps.flows.tests.support import connection_for, graph, node, published_flow
from apps.flows.triggers.platforms import platforms_for_flow

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
#: A send with buttons. SMS has none, so this is the graph a capability warning
#: is actually about.
BUTTONS = {
    "blocks": [{"type": "text", "text": "hello"}],
    "buttons": [{"id": "yes", "label": "Yes", "action": "postback"}],
}


def _detail(client, tenancy, flow):
    url = reverse("flows:api_detail", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})
    return client.get(url).json()


def _trigger(flow, trigger_type=TriggerType.KEYWORD, *, connection=None, enabled=True, config=None):
    trigger = Trigger(
        flow=flow,
        channel_connection=connection,
        type=trigger_type,
        config_json=config if config is not None else {"keywords": [{"text": "help", "mode": "contains"}]},
        enabled=enabled,
    )
    trigger.save()
    return trigger


@pytest.mark.django_db
class TestTheTriggersKey:
    def test_it_is_present_even_when_empty(self, tenancy, client_for):
        """The rule TestPicklists pins for picklists, for the same reason: a
        client that branches on a key's presence breaks the day it arrives."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        payload = _detail(client_for(tenancy.owner), tenancy, flow)

        assert payload["triggers"] == []

    def test_a_summary_carries_what_the_toolbar_needs(self, tenancy, client_for):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        connection = connection_for(tenancy.workspace, external_id="bot-1")
        _trigger(flow, connection=connection)

        row = _detail(client_for(tenancy.owner), tenancy, flow)["triggers"][0]

        assert row["type"] == TriggerType.KEYWORD
        assert row["type_label"] == "Keyword"
        assert row["enabled"] is True
        assert row["connection"]["platform"] == Platform.TELEGRAM
        assert row["platforms"] == [Platform.TELEGRAM]
        assert "help" in row["summary"]

    def test_an_unbound_trigger_reports_a_null_connection(self, tenancy, client_for):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        connection_for(tenancy.workspace, external_id="bot-1")
        _trigger(flow)

        row = _detail(client_for(tenancy.owner), tenancy, flow)["triggers"][0]

        assert row["connection"] is None
        assert row["platforms"] == [Platform.TELEGRAM]

    def test_the_raw_config_is_not_shipped(self, tenancy, client_for):
        """The builder does not edit triggers; the drawer does. Shipping the
        config would make the React store a second place it lives."""
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        _trigger(flow)

        row = _detail(client_for(tenancy.owner), tenancy, flow)["triggers"][0]

        assert "config_json" not in row
        assert "config" not in row

    def test_a_viewer_sees_the_same_list(self, tenancy, client_for):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        _trigger(flow)

        assert len(_detail(client_for(tenancy.user_for("viewer")), tenancy, flow)["triggers"]) == 1

    def test_they_arrive_in_match_order(self, tenancy, client_for):
        flow = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]))
        late = _trigger(flow, TriggerType.WELCOME, config={})
        late.priority = 20
        late.save(update_fields=["priority"])
        early = _trigger(flow)

        rows = _detail(client_for(tenancy.owner), tenancy, flow)["triggers"]

        assert [row["id"] for row in rows] == [str(early.pk), str(late.pk)]


@pytest.mark.django_db
class TestCapabilityWarnings:
    def _flow(self, tenancy):
        return published_flow(tenancy.workspace, graph([node("a", "send_message", BUTTONS)]), name="Buttons")

    def test_a_flow_with_no_triggers_falls_back_to_the_workspace(self, tenancy):
        """Exactly the answer this gave before triggers existed."""
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550006")
        flow = self._flow(tenancy)

        assert platforms_for_flow(flow) == (Platform.SMS,)

    def test_binding_to_sms_warns_about_buttons(self, tenancy, client_for):
        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550007")
        connection_for(tenancy.workspace, external_id="bot-1")
        flow = self._flow(tenancy)
        _trigger(flow, connection=sms)

        payload = _detail(client_for(tenancy.owner), tenancy, flow)

        assert any(issue["code"] == "capability_unsupported" for issue in payload["validation"]["warnings"])

    def test_binding_to_telegram_does_not(self, tenancy, client_for):
        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550008")
        flow = self._flow(tenancy)
        _trigger(flow, connection=telegram)

        payload = _detail(client_for(tenancy.owner), tenancy, flow)

        assert not any(issue["code"] == "capability_unsupported" for issue in payload["validation"]["warnings"])

    def test_a_disabled_trigger_does_not_narrow_the_platforms(self, tenancy):
        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550009")
        flow = self._flow(tenancy)
        _trigger(flow, connection=telegram, enabled=False)

        assert platforms_for_flow(flow) == tuple(sorted({Platform.SMS, Platform.TELEGRAM}))
        assert sms.pk  # the SMS connection is what makes the fallback observable

    def test_an_unbound_trigger_narrows_to_what_its_type_runs_on(self, tenancy):
        connection_for(tenancy.workspace, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550010")
        flow = self._flow(tenancy)
        _trigger(flow, TriggerType.WELCOME, config={})

        # welcome is telegram and messenger; only telegram is connected here.
        assert platforms_for_flow(flow) == (Platform.TELEGRAM,)

    def test_an_api_only_flow_falls_back(self, tenancy):
        """An `api` trigger names no channel, so the flow can start on any of them."""
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550011")
        flow = self._flow(tenancy)
        _trigger(flow, TriggerType.API, config={})

        assert platforms_for_flow(flow) == (Platform.SMS,)

    def test_an_api_trigger_beside_a_bound_one_widens_the_scope_back(self, tenancy):
        """``fire_api_trigger`` resolves a channel from the contact's own
        identities, so a flow carrying an api trigger really can run on SMS —
        letting the Telegram trigger beside it narrow the set would suppress
        warnings for a run that can genuinely happen."""
        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550015")
        flow = self._flow(tenancy)
        _trigger(flow, connection=telegram)
        _trigger(flow, TriggerType.API, config={})

        assert platforms_for_flow(flow) == tuple(sorted({Platform.SMS, Platform.TELEGRAM}))

    def test_a_disabled_api_trigger_does_not_widen_it(self, tenancy):
        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550016")
        flow = self._flow(tenancy)
        _trigger(flow, connection=telegram)
        _trigger(flow, TriggerType.API, config={}, enabled=False)

        assert platforms_for_flow(flow) == (Platform.TELEGRAM,)

    def test_a_bound_trigger_counts_even_while_the_connection_needs_reauth(self, tenancy):
        """The binding is the author's stated intent; needs_reauth is a temporary
        condition, and dropping the warning while a token refreshes would be
        worse than useless."""
        from apps.channels.models import ConnectionStatus

        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550012")
        sms.status = ConnectionStatus.NEEDS_REAUTH
        sms.save(update_fields=["status"])
        flow = self._flow(tenancy)
        _trigger(flow, connection=sms)

        assert platforms_for_flow(flow) == (Platform.SMS,)

    def test_the_workspace_form_still_works_without_a_flow(self, tenancy):
        """The kwarg is additive: a caller holding only a workspace is unchanged."""
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550013")
        document = graph([node("a", "send_message", BUTTONS)])

        result = validate_for_workspace(document, tenancy.workspace)

        assert any(issue.code == "capability_unsupported" for issue in result.warnings)

    def test_publish_uses_the_flows_platforms(self, tenancy):
        from apps.flows.services import publish

        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550014")
        flow = self._flow(tenancy)
        _trigger(flow, connection=telegram)

        result = publish(flow)

        assert not any(issue.code == "capability_unsupported" for issue in result.validation.warnings)
