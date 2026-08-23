"""The rules manager: CRUD, priority, and the dry-run (SPEC §14).

``manage_workspace_settings``, not ``reply_in_inbox``. A rule reassigns and
closes conversations across the whole workspace with nobody watching, and every
other settings CRUD in this product sits above Agent for the same reason — the
contacts tag editor is ``manage_crm``, triggers are ``edit_flows``, outbound
webhooks are this key. ``PERMISSION_KEYS`` is the whole vocabulary; nothing here
invents one.

The dry-run's own acceptance criterion — "dry-run matches live behaviour" — is
covered by :class:`TestTheDryRun` below, and it is a *property* rather than a
promise: both callers score the same
:class:`~apps.inbox.rules.RuleInput` through the same
:func:`~apps.inbox.rules.matches_shallow`.
"""

from typing import Any

import pytest

from apps.inbox import selectors, services
from apps.inbox.models import ConversationLabel, InboxRule

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(tenancy: Any, client_for: Any) -> Any:
    return client_for(tenancy.user_for("admin"))


def _rule(workspace: Any, name: str = "Refunds", **overrides: Any) -> InboxRule:
    values: dict[str, Any] = {
        "condition_json": {"keywords": [{"text": "refund", "mode": "contains"}]},
        "actions_json": [{"type": "mark_done"}],
        "enabled": True,
    }
    values.update(overrides)
    rule = InboxRule(workspace=workspace, name=name, **values)
    rule.save()
    return rule


class TestPermissions:
    def test_an_agent_cannot_open_the_rules_page(self, agent_client, url_for):
        assert agent_client.get(url_for("rule_settings")).status_code == 403

    def test_an_agent_cannot_save_a_rule(self, agent_client, url_for):
        assert agent_client.post(url_for("rule_save"), {"name": "Sneaky"}).status_code == 403

    def test_an_admin_can(self, admin_client, url_for):
        assert admin_client.get(url_for("rule_settings")).status_code == 200


class TestSaving:
    def test_it_stores_all_three_condition_halves(self, tenancy, admin_client, url_for, connection):
        import json

        from apps.contacts.models import Tag

        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        label = services.create_label(tenancy.workspace, name="Refunds")
        contact_half = json.dumps({"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]})

        response = admin_client.post(
            url_for("rule_save"),
            {
                "name": "Refund requests",
                "enabled": "on",
                "platform": ["telegram"],
                "connection": [str(connection.pk)],
                "keyword_text": ["refund"],
                "keyword_mode": ["contains"],
                "filter": contact_half,
                "action_label": [str(label.pk)],
                "action_done": "on",
            },
        )

        assert response.status_code == 204
        rule = InboxRule.objects.for_workspace(tenancy.workspace).get()
        assert rule.condition_json["channel"]["platforms"] == ["telegram"]
        assert rule.condition_json["keywords"] == [{"text": "refund", "mode": "contains"}]
        assert rule.condition_json["contact"]["rules"][0]["source"] == "tag"
        assert [action["type"] for action in rule.actions_json] == ["add_label", "mark_done"]

    def test_a_rule_with_no_condition_is_refused(self, tenancy, admin_client, url_for):
        """`match: all` over zero rules matches everyone, and the builder's empty
        state is exactly that — so this is the one place "label every message in
        the workspace" could be created by accident."""
        response = admin_client.post(url_for("rule_save"), {"name": "Everything", "action_done": "on"})

        assert response.status_code == 204
        assert not InboxRule.objects.for_workspace(tenancy.workspace).exists()

    def test_a_rule_with_no_action_is_refused(self, tenancy, admin_client, url_for):
        response = admin_client.post(
            url_for("rule_save"),
            {"name": "Pointless", "keyword_text": ["refund"], "keyword_mode": ["contains"]},
        )

        assert response.status_code == 204
        assert not InboxRule.objects.for_workspace(tenancy.workspace).exists()

    def test_a_label_from_another_workspace_is_refused(self, tenancy, other_tenancy, admin_client, url_for):
        theirs = ConversationLabel.objects.create(workspace=other_tenancy.workspace, name="Theirs")

        admin_client.post(
            url_for("rule_save"),
            {
                "name": "Cross tenant",
                "keyword_text": ["refund"],
                "keyword_mode": ["contains"],
                "action_label": [str(theirs.pk)],
            },
        )

        assert not InboxRule.objects.for_workspace(tenancy.workspace).exists()

    def test_editing_keeps_the_same_row(self, tenancy, admin_client, url_for):
        rule = _rule(tenancy.workspace)

        admin_client.post(
            url_for("rule_save"),
            {
                "rule": str(rule.pk),
                "name": "Renamed",
                "keyword_text": ["refund"],
                "keyword_mode": ["contains"],
                "action_done": "on",
            },
        )

        rule.refresh_from_db()
        assert rule.name == "Renamed"
        assert InboxRule.objects.for_workspace(tenancy.workspace).count() == 1

    def test_editing_another_workspaces_rule_is_a_404(self, tenancy, other_tenancy, admin_client, url_for):
        theirs = _rule(other_tenancy.workspace, "Theirs")

        response = admin_client.post(url_for("rule_save"), {"rule": str(theirs.pk), "name": "Mine now"})

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.name == "Theirs"


class TestOrdering:
    def test_reordering_renumbers_to_the_step(self, tenancy, admin_client, url_for):
        first = _rule(tenancy.workspace, "First", priority=0)
        second = _rule(tenancy.workspace, "Second", priority=10)

        admin_client.post(url_for("rule_reorder"), {"rule": [str(second.pk), str(first.pk)]})

        first.refresh_from_db()
        second.refresh_from_db()
        assert (second.priority, first.priority) == (0, services.PRIORITY_STEP)

    def test_an_id_from_another_workspace_is_ignored(self, tenancy, other_tenancy, admin_client, url_for):
        """The drag handle posts whatever the DOM held, so an unreachable id is
        ordinary rather than an attack — and refusing the whole reorder over one
        would be an error the operator cannot act on."""
        mine = _rule(tenancy.workspace, "Mine")
        theirs = _rule(other_tenancy.workspace, "Theirs", priority=99)

        response = admin_client.post(url_for("rule_reorder"), {"rule": [str(theirs.pk), str(mine.pk)]})

        assert response.status_code == 204
        theirs.refresh_from_db()
        assert theirs.priority == 99

    def test_toggling_flips_enabled(self, tenancy, admin_client, url_for):
        rule = _rule(tenancy.workspace)

        admin_client.post(url_for("rule_toggle", rule_id=rule.pk))

        rule.refresh_from_db()
        assert rule.enabled is False


class TestTheDryRun:
    def test_it_finds_the_message_that_would_have_matched(self, tenancy, admin_client, url_for, conversation, inbound):
        inbound(text="i would like a refund")
        inbound(text="what are your hours")

        response = admin_client.post(url_for("rule_test"), {"keyword_text": ["refund"], "keyword_mode": ["contains"]})

        assert response.status_code == 200
        assert response.context["sample"] == 2
        assert len(response.context["matches"]) == 1

    def test_it_agrees_with_the_live_hook(self, tenancy, conversation, connection, identity, inbound):
        """The acceptance criterion, as a property rather than a promise: the
        message that fires the rule live is exactly the one the dry-run names,
        because both go through the same matcher over the same RuleInput."""
        from apps.flows.tests.routing_support import routing_adapter
        from apps.flows.tests.support import inbound as raw_event
        from apps.flows.triggers.pipeline import route_events
        from apps.inbox.models import InboxRuleApplication

        condition = {"keywords": [{"text": "refund", "mode": "contains"}]}
        rule = _rule(tenancy.workspace, condition_json=condition)
        stored = inbound(text="i would like a refund")

        matched, _ = selectors.dry_run(tenancy.workspace, condition)
        assert [message.pk for message in matched] == [stored.pk]

        with routing_adapter(connection.platform):
            route_events(connection, [raw_event(connection, text="i would like a refund", user="u1")])

        applied = InboxRuleApplication.objects.for_workspace(tenancy.workspace).get()
        assert applied.rule_id == rule.pk

    def test_it_writes_nothing(self, tenancy, admin_client, url_for, conversation, inbound):
        """Structural, not careful: the view never reaches apps.inbox.routing,
        which is where applying lives."""
        from apps.inbox.models import ConversationLabelLink, InboxRuleApplication

        inbound(text="i would like a refund")
        label = services.create_label(tenancy.workspace, name="Refunds")

        admin_client.post(
            url_for("rule_test"),
            {
                "keyword_text": ["refund"],
                "keyword_mode": ["contains"],
                "action_label": [str(label.pk)],
            },
        )

        assert not ConversationLabelLink.objects.for_workspace(tenancy.workspace).exists()
        assert not InboxRuleApplication.objects.for_workspace(tenancy.workspace).exists()

    def test_it_skips_the_teams_own_replies(self, tenancy, admin_client, url_for, conversation, outbound):
        """The hook only ever sees inbound events, so a dry-run that scored the
        agent's own messages would look broken."""
        outbound(text="here is your refund")

        response = admin_client.post(url_for("rule_test"), {"keyword_text": ["refund"], "keyword_mode": ["contains"]})

        assert response.context["sample"] == 0

    def test_the_contact_half_is_one_query_per_rule_not_one_per_message(
        self, tenancy, conversation, inbound, django_assert_max_num_queries
    ):
        """Fifty messages and five rules is five condition-engine queries this
        way and two hundred and fifty the other."""
        from apps.contacts.models import Tag

        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        for index in range(10):
            inbound(text=f"refund {index}", key=f"in:{index}")
        condition = {
            "keywords": [{"text": "refund", "mode": "contains"}],
            "contact": {"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]},
        }

        # One for the sample, one for the whole contact half, plus the engine's
        # own compile-time lookups — nowhere near one per message.
        with django_assert_max_num_queries(8):
            selectors.dry_run(tenancy.workspace, condition)

    def test_a_broken_condition_is_reported_rather_than_raised(self, tenancy, admin_client, url_for):
        response = admin_client.post(
            url_for("rule_test"), {"filter": '{"match": "all", "rules": [{"source": "nope", "key": "x", "op": "has"}]}'}
        )

        assert response.status_code == 200
        assert response.context["error"]


class TestTheEditor:
    def test_it_renders_with_the_shared_condition_builder(self, tenancy, admin_client, url_for):
        """The editor embeds ``templates/contacts/_filter_bar.html`` — the same
        builder the segment editor uses, driven by the same payload from
        ``apps.contacts.builder.builder_config``. A smoke test, because the two
        templates are only wired together at render time and a missing context
        key is invisible until somebody opens the page."""
        response = admin_client.get(url_for("rule_form"))

        assert response.status_code == 200
        assert b"contactFilters" in response.content or b"crm-filters" in response.content
        assert response.context["filter_config"]["vocabulary"]

    def test_it_hydrates_an_existing_rule(self, tenancy, admin_client, url_for):
        rule = _rule(tenancy.workspace, "Refunds")

        response = admin_client.get(url_for("rule_form"), {"rule": str(rule.pk)})

        assert response.status_code == 200
        assert response.context["rule"] == rule
        assert response.context["condition"]["keywords"][0]["text"] == "refund"

    def test_another_workspaces_rule_is_a_404(self, tenancy, other_tenancy, admin_client, url_for):
        theirs = _rule(other_tenancy.workspace, "Theirs")

        assert admin_client.get(url_for("rule_form"), {"rule": str(theirs.pk)}).status_code == 404

    def test_the_settings_page_itself_renders(self, tenancy, admin_client, url_for):
        """Covers the sortable/keyboard reorder script and the rows partial."""
        _rule(tenancy.workspace, "Refunds")

        page = admin_client.get(url_for("rule_settings")).content.decode()

        assert "data-rule-list" in page
        assert "data-rule-move" in page
        assert "Message mentions refund" in page


class TestEditingPreservesActions:
    def test_reopening_a_rule_preselects_its_actions(self, tenancy, admin_client, url_for):
        """The regression this class exists for: the editor hydrated its
        keywords and channel selects and left the three action controls blank,
        so an edit silently forgot what the rule did."""
        label = services.create_label(tenancy.workspace, name="Refunds")
        agent = tenancy.user_for("agent")
        rule = _rule(
            tenancy.workspace,
            actions_json=[
                {"type": "add_label", "label_id": str(label.pk)},
                {"type": "assign_to_member", "user_id": str(agent.pk)},
                {"type": "mark_done"},
            ],
        )

        response = admin_client.get(url_for("rule_form"), {"rule": str(rule.pk)})

        selected = response.context["selected_actions"]
        assert selected["label_ids"] == [str(label.pk)]
        assert selected["assignee_id"] == str(agent.pk)
        assert selected["mark_done"] is True

        page = response.content.decode()
        assert f'value="{label.pk}"\n                  selected' in page or "selected" in page
        assert page.count("selected") >= 2

    def test_renaming_a_rule_through_the_form_keeps_its_actions(self, tenancy, admin_client, url_for):
        """End to end: read the editor, post back exactly what it rendered, and
        the actions survive."""
        label = services.create_label(tenancy.workspace, name="Refunds")
        rule = _rule(
            tenancy.workspace,
            actions_json=[{"type": "add_label", "label_id": str(label.pk)}, {"type": "mark_done"}],
        )
        selected = admin_client.get(url_for("rule_form"), {"rule": str(rule.pk)}).context["selected_actions"]

        response = admin_client.post(
            url_for("rule_save"),
            {
                "rule": str(rule.pk),
                "name": "Renamed",
                "enabled": "on",
                "keyword_text": ["refund"],
                "keyword_mode": ["contains"],
                # Exactly what the hydrated form would submit.
                "action_label": selected["label_ids"],
                "action_assignee": selected["assignee_id"],
                "action_done": "on" if selected["mark_done"] else "",
            },
        )

        assert response.status_code == 204
        rule.refresh_from_db()
        assert rule.name == "Renamed"
        assert [action["type"] for action in rule.actions_json] == ["add_label", "mark_done"]

    def test_the_editor_does_not_repeat_the_container_id(self, tenancy, admin_client, url_for):
        """It is swapped into #inbox-rule-editor with innerHTML, so carrying that
        id would put two elements with one id in the document and send every
        later hx-target to the wrapper."""
        page = admin_client.get(url_for("rule_form")).content.decode()

        assert 'id="inbox-rule-editor"' not in page
