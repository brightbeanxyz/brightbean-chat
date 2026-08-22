"""SPEC §10's matcher: priority order, first match wins, and the keyword modes."""

import pytest

from apps.channels.events import EventType
from apps.common.platforms import Platform
from apps.flows.models import Trigger, TriggerType
from apps.flows.services import create_flow, save_draft
from apps.flows.tests.support import connection_for, graph, inbound, node, published_flow
from apps.flows.triggers import keywords
from apps.flows.triggers.matching import EVENT_TRIGGER_TYPES, MatchContext, match, registered_matchers
from apps.flows.triggers.types import STUB_TYPES

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def _flow(workspace, name):
    return published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)


def _trigger(flow, trigger_type, config=None, *, priority=0, connection=None, enabled=True):
    trigger = Trigger(
        flow=flow,
        channel_connection=connection,
        type=trigger_type,
        config_json=config or {},
        priority=priority,
        enabled=enabled,
    )
    trigger.save()
    return trigger


def _context(connection, **kwargs):
    return MatchContext.from_event(connection, inbound(connection, **kwargs))


class TestKeywordModes:
    @pytest.mark.parametrize(
        ("text", "mode", "expected"),
        [
            ("help", "exact", True),
            ("  HELP  ", "exact", True),
            ("help me", "exact", False),
            ("help me", "contains", True),
            ("category", "contains", True),
            # The one distinction between contains and any_word, and the thing
            # users complain about when it is wrong.
            ("category", "any_word", False),
            ("my cat sleeps", "any_word", True),
            ("CAT", "any_word", True),
        ],
    )
    def test_the_three_modes(self, text, mode, expected):
        needle = "help" if mode == "exact" or "help" in text else "cat"
        assert keywords.matches_any(text, [{"text": needle, "mode": mode}]) is expected

    def test_matching_is_case_folded_not_lowercased(self):
        """casefold() is what makes ß and İ behave; lower() does not."""
        assert keywords.matches_any("STRASSE", [{"text": "strasse", "mode": "exact"}])

    def test_an_empty_message_matches_nothing(self):
        assert keywords.matches_any("", [{"text": "help", "mode": "contains"}]) is False

    def test_text_is_capped(self):
        haystack = ("x" * keywords.MAX_MATCH_CHARS) + " help"
        assert keywords.matches_any(haystack, [{"text": "help", "mode": "any_word"}]) is False

    def test_a_malformed_keyword_entry_is_skipped_not_raised(self):
        assert keywords.matches_any("help", ["help", None, {"mode": "exact"}]) is False


@pytest.mark.django_db
class TestFirstMatchWins:
    def test_the_lower_priority_trigger_wins_an_overlap(self, tenancy, connection):
        """Two keyword triggers both matching "help"; SPEC §10 says lower first."""
        first = _flow(tenancy.workspace, "First")
        second = _flow(tenancy.workspace, "Second")
        winner = _trigger(first, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]}, priority=0)
        _trigger(second, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]}, priority=10)

        found = match(_context(connection, text="help please"))

        assert found is not None
        assert found.trigger.pk == winner.pk

    def test_reversing_the_priorities_reverses_the_winner(self, tenancy, connection):
        first = _flow(tenancy.workspace, "First")
        second = _flow(tenancy.workspace, "Second")
        _trigger(first, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]}, priority=10)
        winner = _trigger(second, TriggerType.KEYWORD, {"keywords": [{"text": "help", "mode": "contains"}]}, priority=0)

        found = match(_context(connection, text="help please"))

        assert found.trigger.pk == winner.pk

    def test_a_tie_is_broken_deterministically(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        first = _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "contains"}]}, priority=0)
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "contains"}]}, priority=0)

        for _ in range(3):
            assert match(_context(connection, text="hi")).trigger.pk == first.pk

    def test_a_disabled_trigger_never_matches(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "exact"}]}, enabled=False)

        assert match(_context(connection, text="hi")) is None

    def test_a_trigger_on_an_unpublished_flow_is_not_a_candidate(self, tenancy, connection):
        """It must not *win* and then swallow the event when start_flow refuses —
        the trigger that should have matched next would never run."""
        draft = create_flow(workspace=tenancy.workspace, name="Draft")
        save_draft(draft, graph([node("a", "action", NOOP_ACTION)]))
        _trigger(draft, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "exact"}]}, priority=0)

        live = _flow(tenancy.workspace, "Live")
        winner = _trigger(live, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "exact"}]}, priority=10)

        assert match(_context(connection, text="hi")).trigger.pk == winner.pk


@pytest.mark.django_db
class TestConnectionBinding:
    def test_a_bound_trigger_only_fires_on_its_own_connection(self, tenancy, connection):
        other = connection_for(tenancy.workspace, external_id="bot-b")
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "exact"}]}, connection=other)

        assert match(_context(connection, text="hi")) is None
        assert match(_context(other, text="hi")) is not None

    def test_an_unbound_trigger_fires_on_a_matching_platform(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.WELCOME)

        assert match(_context(connection, kind=EventType.REFERRAL)) is not None

    def test_an_unbound_trigger_skips_a_platform_its_type_does_not_run_on(self, tenancy):
        """SPEC §5's null connection means "all connections of *matching*
        platform" — and welcome is Telegram and Messenger only."""
        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550001")
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.WELCOME)

        assert match(_context(sms, kind=EventType.REFERRAL)) is None

    def test_another_workspace_cannot_be_matched(self, tenancy, other_tenancy, connection):
        theirs = _flow(other_tenancy.workspace, "Theirs")
        _trigger(theirs, TriggerType.KEYWORD, {"keywords": [{"text": "hi", "mode": "exact"}]})

        assert match(_context(connection, text="hi")) is None


@pytest.mark.django_db
class TestPerTypeMatchers:
    def test_ref_url_matches_the_exact_ref(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.REF_URL, {"ref": "promo"})

        assert match(_context(connection, kind=EventType.REFERRAL, ref="promo")) is not None
        assert match(_context(connection, kind=EventType.REFERRAL, ref="promo2")) is None

    def test_a_matched_ref_reaches_the_flow_as_a_variable(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.REF_URL, {"ref": "promo"})

        found = match(_context(connection, kind=EventType.REFERRAL, ref="promo"))
        assert found.variables["trigger_ref"] == "promo"

    def test_welcome_matches_a_referral_with_no_ref(self, tenancy, connection):
        """The normalised shape of Telegram's `/start` with no payload."""
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.WELCOME)

        assert match(_context(connection, kind=EventType.REFERRAL, ref="")) is not None
        assert match(_context(connection, kind=EventType.REFERRAL, ref="promo")) is None

    def test_welcome_matches_a_get_started_postback(self, tenancy, connection):
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(flow, TriggerType.WELCOME)

        assert match(_context(connection, kind=EventType.POSTBACK, button_id="get_started")) is not None

    def test_comment_scope_include_and_exclude(self, tenancy):
        instagram = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        flow = _flow(tenancy.workspace, "Flow")
        _trigger(
            flow,
            TriggerType.COMMENT,
            {
                "post_scope": "specific",
                "post_ids": ["p-1"],
                "include_keywords": ["price"],
                "exclude_keywords": ["spam"],
                "top_level_only": True,
            },
        )

        def comment(text, post_id="p-1", parent=""):
            return _context(
                instagram,
                kind=EventType.COMMENT,
                text=text,
                extra={"post_id": post_id, "parent_comment_id": parent},
            )

        assert match(comment("what is the price?")) is not None
        assert match(comment("what is the price?", post_id="p-2")) is None
        assert match(comment("nice photo")) is None
        assert match(comment("price spam")) is None
        assert match(comment("price?", parent="c-9")) is None


class TestTheRegistryItself:
    def test_the_stub_types_are_registered_and_decline(self):
        """A pin, like engine.registry.types_without_runtime(): a type leaving
        this set is L5-A's deliberate act with a test to update, not a silent
        behaviour change on somebody else's branch."""
        assert {TriggerType.STORY_MENTION, TriggerType.STORY_REPLY, TriggerType.FOLLOW} == STUB_TYPES
        assert set(registered_matchers()) >= STUB_TYPES

    def test_api_has_no_matcher(self):
        """SPEC §10: fired only through the public flow-start endpoint."""
        assert TriggerType.API not in registered_matchers()

    def test_rule_has_no_matcher(self):
        """L6-A consumes the internal event catalogue, not this pipeline."""
        assert TriggerType.RULE not in registered_matchers()

    def test_default_reply_is_never_a_candidate(self):
        """It is SPEC §9.3 step 4 — a stage after everything declined, not a peer."""
        for types in EVENT_TRIGGER_TYPES.values():
            assert TriggerType.DEFAULT_REPLY not in types

    def test_neither_is_rule_or_api(self):
        for types in EVENT_TRIGGER_TYPES.values():
            assert TriggerType.RULE not in types
            assert TriggerType.API not in types

    def test_a_delivery_receipt_selects_nothing(self):
        assert EventType.DELIVERY_STATUS not in EVENT_TRIGGER_TYPES
