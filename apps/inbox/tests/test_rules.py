"""The rule engine itself: what matches, and what may be stored (SPEC §14).

No hook and no HTTP in this module on purpose — the acceptance criterion
"dry-run matches live behaviour" is only true if one function answers both, so
that function is worth testing on its own before either caller exists.
"""

from typing import Any

import pytest

from apps.contacts.conditions import ConditionValidationError
from apps.contacts.models import Contact, ContactTag, Tag
from apps.inbox import rules
from apps.inbox.models import ConversationLabel

pytestmark = pytest.mark.django_db


def _rule(workspace: Any, **overrides: Any) -> Any:
    from apps.inbox.models import InboxRule

    values: dict[str, Any] = {"name": "Rule", "condition_json": {}, "actions_json": []}
    values.update(overrides)
    return InboxRule(workspace=workspace, **values)


def _input(**overrides: Any) -> rules.RuleInput:
    values: dict[str, Any] = {
        "text": "i would like a refund please",
        "platform": "telegram",
        "connection_id": "11111111-1111-1111-1111-111111111111",
        "contact": None,
    }
    values.update(overrides)
    return rules.RuleInput(**values)


class TestMatching:
    def test_a_keyword_clause_uses_the_shared_matcher(self, tenancy):
        """SPEC §10's three modes, not a fourth spelling of them.

        `any_word` is the mode that distinguishes the two implementations: it
        matches "my cat sleeps" and not "category", which a `contains` clone
        would get wrong in exactly one direction.
        """
        compiled = rules.compile_rule(
            _rule(tenancy.workspace, condition_json={"keywords": [{"text": "cat", "mode": "any_word"}]})
        )

        assert rules.matches(compiled, _input(text="my cat sleeps"))
        assert not rules.matches(compiled, _input(text="the category page"))

    def test_an_absent_clause_is_not_a_constraint(self, tenancy):
        compiled = rules.compile_rule(_rule(tenancy.workspace, condition_json={"channel": {"platforms": ["telegram"]}}))

        assert rules.matches(compiled, _input(text="anything at all"))

    def test_the_clauses_are_anded(self, tenancy):
        compiled = rules.compile_rule(
            _rule(
                tenancy.workspace,
                condition_json={
                    "channel": {"platforms": ["whatsapp"]},
                    "keywords": [{"text": "refund", "mode": "contains"}],
                },
            )
        )

        assert not rules.matches(compiled, _input(platform="telegram"))
        assert rules.matches(compiled, _input(platform="whatsapp"))

    def test_platform_and_connection_are_independent_filters(self, tenancy):
        """Two multi-selects in a form mean an AND, not an OR."""
        compiled = rules.compile_rule(
            _rule(
                tenancy.workspace,
                condition_json={
                    "channel": {
                        "platforms": ["telegram"],
                        "connection_ids": ["22222222-2222-2222-2222-222222222222"],
                    }
                },
            )
        )

        assert not rules.matches(compiled, _input(platform="telegram"))
        assert rules.matches(
            compiled, _input(platform="telegram", connection_id="22222222-2222-2222-2222-222222222222")
        )

    def test_a_contact_clause_goes_through_the_condition_engine(self, tenancy):
        """ROADMAP contract 8: one filter language, and inbox rules are a consumer."""
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        vip = Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")
        ContactTag(contact=vip, tag=tag).save()
        stranger = Contact.objects.create(workspace=tenancy.workspace, first_name="Bob")
        compiled = rules.compile_rule(
            _rule(
                tenancy.workspace,
                condition_json={
                    "contact": {"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]}
                },
            )
        )

        assert rules.matches(compiled, _input(contact=vip))
        assert not rules.matches(compiled, _input(contact=stranger))

    def test_a_contact_clause_cannot_be_true_of_nobody(self, tenancy):
        """The engine's negatives include absence, so `has_not` over a missing
        contact would otherwise fire — a different statement from the one the
        operator wrote."""
        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        compiled = rules.compile_rule(
            _rule(
                tenancy.workspace,
                condition_json={
                    "contact": {"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has_not"}]}
                },
            )
        )

        assert not rules.matches(compiled, _input(contact=None))

    def test_shallow_matching_touches_no_database(self, tenancy, django_assert_num_queries):
        """What lets the dry-run ask the engine once per rule instead of once
        per message per rule."""
        compiled = rules.compile_rule(
            _rule(tenancy.workspace, condition_json={"keywords": [{"text": "refund", "mode": "contains"}]})
        )

        with django_assert_num_queries(0):
            assert rules.matches_shallow(compiled, _input())


class TestRuleInput:
    def test_both_constructors_produce_the_same_haystack(self, tenancy, conversation, inbound):
        """The dry-run's whole claim. A message and the event that produced it
        must normalise to one string, or the two callers disagree by
        construction."""
        message = inbound(text="  I would like a REFUND  ")

        from_message = rules.RuleInput.from_message(message)

        assert from_message.text == "i would like a refund"
        assert from_message.platform == str(message.channel_connection.platform)
        assert from_message.contact == conversation.contact


class TestConditionValidation:
    def test_it_refuses_a_rule_with_no_clause_at_all(self, tenancy):
        """`match: all` over zero rules matches everyone, and the builder's
        empty state is exactly that — so this is the one place "label every
        message in the workspace" can be created by accident."""
        with pytest.raises(rules.RuleValidationError):
            rules.validate_condition(tenancy.workspace, {})

    def test_it_names_an_unknown_key_rather_than_dropping_it(self, tenancy):
        with pytest.raises(rules.RuleValidationError, match="assignee"):
            rules.validate_condition(tenancy.workspace, {"assignee": "someone"})

    def test_it_rejects_an_unknown_platform(self, tenancy):
        with pytest.raises(rules.RuleValidationError, match="platform"):
            rules.validate_condition(tenancy.workspace, {"channel": {"platforms": ["carrier-pigeon"]}})

    def test_it_dedupes_keywords_case_insensitively(self, tenancy):
        document = rules.validate_condition(
            tenancy.workspace,
            {"keywords": [{"text": "Refund", "mode": "contains"}, {"text": "refund", "mode": "contains"}]},
        )

        assert document["keywords"] == [{"text": "Refund", "mode": "contains"}]

    def test_it_caps_the_document_size(self, tenancy):
        with pytest.raises(rules.RuleValidationError, match="too large"):
            rules.validate_condition(
                tenancy.workspace,
                {"keywords": [{"text": "x" * 150, "mode": "contains"} for _ in range(90)]},
            )

    def test_the_contact_half_is_the_engines_to_refuse(self, tenancy):
        """Not caught and re-wrapped: the engine's message is more specific than
        anything this module could say about it."""
        with pytest.raises(ConditionValidationError):
            rules.validate_condition(
                tenancy.workspace,
                {"contact": {"match": "all", "rules": [{"source": "nonsense", "key": "x", "op": "has"}]}},
            )


class TestActionValidation:
    def test_it_resolves_every_id_inside_the_workspace(self, tenancy, other_tenancy):
        """A form naming another tenant's label is refused here, because the
        hook that acts on it later has no request to check against."""
        theirs = ConversationLabel.objects.create(workspace=other_tenancy.workspace, name="Theirs")

        with pytest.raises(rules.RuleValidationError, match="no longer exists"):
            rules.validate_actions(tenancy.workspace, [{"type": "add_label", "label_id": str(theirs.pk)}])

    def test_it_refuses_a_member_of_another_workspace(self, tenancy, other_tenancy):
        with pytest.raises(rules.RuleValidationError, match="not a member"):
            rules.validate_actions(
                tenancy.workspace,
                [{"type": "assign_to_member", "user_id": str(other_tenancy.owner.pk)}],
            )

    def test_it_refuses_two_assignees(self, tenancy):
        """A rule whose outcome depended on which action ran last."""
        first = tenancy.user_for("agent")
        second = tenancy.user_for("admin")

        with pytest.raises(rules.RuleValidationError, match="repeats"):
            rules.validate_actions(
                tenancy.workspace,
                [
                    {"type": "assign_to_member", "user_id": str(first.pk)},
                    {"type": "assign_to_member", "user_id": str(second.pk)},
                ],
            )

    def test_it_refuses_an_unknown_verb(self, tenancy):
        with pytest.raises(rules.RuleValidationError, match="delete_everything"):
            rules.validate_actions(tenancy.workspace, [{"type": "delete_everything"}])

    def test_it_refuses_an_empty_action_list(self, tenancy):
        with pytest.raises(rules.RuleValidationError, match="at least one action"):
            rules.validate_actions(tenancy.workspace, [])

    def test_it_keeps_a_valid_list(self, tenancy):
        label = ConversationLabel.objects.create(workspace=tenancy.workspace, name="Refunds")
        agent = tenancy.user_for("agent")

        actions = rules.validate_actions(
            tenancy.workspace,
            [
                {"type": "add_label", "label_id": str(label.pk)},
                {"type": "assign_to_member", "user_id": str(agent.pk)},
                {"type": "mark_done"},
            ],
        )

        assert [item["type"] for item in actions] == ["add_label", "assign_to_member", "mark_done"]
