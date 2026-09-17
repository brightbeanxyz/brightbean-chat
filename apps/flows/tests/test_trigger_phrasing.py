"""The flow list's "when does this run" sentences.

Written after the real thing broke and the suite did not notice: every flow in
the fixtures had zero triggers, so `describe_triggers` never reached the branch
that reads a trigger's config and the wrong field name survived until the page
was opened by hand. Every test here builds a flow that actually has triggers.
"""

import pytest

from apps.flows.models import Trigger, TriggerType
from apps.flows.services import create_flow
from apps.flows.triggers.phrasing import describe_trigger, describe_triggers


def _trigger(workspace, flow, trigger_type, config=None, *, enabled=True):
    return Trigger.objects.create(
        workspace=workspace,
        flow=flow,
        type=trigger_type,
        config_json=config or {},
        enabled=enabled,
    )


@pytest.mark.django_db
class TestOneTrigger:
    def test_it_names_the_moment_not_the_mechanism(self, tenancy):
        """ "Comment" is what the product calls it; "when someone comments on a
        post" is when the reader's flow runs."""
        flow = create_flow(workspace=tenancy.workspace, name="Lead magnet")
        trigger = _trigger(tenancy.workspace, flow, TriggerType.COMMENT)

        assert describe_trigger(trigger) == "when someone comments on a post"

    def test_a_keyword_trigger_quotes_its_words(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="FAQ")
        trigger = _trigger(
            tenancy.workspace,
            flow,
            TriggerType.KEYWORD,
            {"keywords": [{"text": "hours", "mode": "any_word"}, {"text": "returns", "mode": "any_word"}]},
        )

        assert describe_trigger(trigger) == "when someone sends “hours” or “returns”"

    def test_a_comment_trigger_reads_its_own_config_key(self, tenancy):
        """A comment trigger stores a plain list under `include_keywords` while
        a keyword trigger stores objects under `keywords`. Both are "words that
        start this flow" to a reader."""
        flow = create_flow(workspace=tenancy.workspace, name="Price")
        trigger = _trigger(tenancy.workspace, flow, TriggerType.COMMENT, {"include_keywords": ["price"]})

        assert describe_trigger(trigger) == "when someone comments “price”"

    def test_a_long_keyword_list_counts_instead_of_listing(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Everything")
        trigger = _trigger(
            tenancy.workspace,
            flow,
            TriggerType.KEYWORD,
            {"keywords": [{"text": word} for word in ["a", "b", "c", "d", "e"]]},
        )

        assert describe_trigger(trigger) == "when someone sends “a” and 4 more"

    def test_an_empty_config_falls_back_to_the_plain_phrase(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Bare")
        trigger = _trigger(tenancy.workspace, flow, TriggerType.KEYWORD, {})

        assert describe_trigger(trigger) == "when someone sends a keyword"

    @pytest.mark.parametrize("trigger_type", list(TriggerType.values))
    def test_every_trigger_type_has_something_to_say(self, tenancy, trigger_type):
        """A type added to the enum without copy must still render a sentence.
        The fallback is wrong in register but right in substance, which beats a
        blank line on a list page."""
        flow = create_flow(workspace=tenancy.workspace, name="Any")
        trigger = _trigger(tenancy.workspace, flow, trigger_type)

        assert describe_trigger(trigger).startswith("when ")


@pytest.mark.django_db
class TestAWholeFlow:
    def test_no_trigger_says_the_flow_will_not_run(self, tenancy):
        """The warning is the point. A flow with no trigger is a flow nothing
        will ever start, and the list is where somebody notices that before
        wondering why nothing happened."""
        flow = create_flow(workspace=tenancy.workspace, name="Unfinished")

        assert describe_triggers(list(flow.triggers.all())) == "No trigger yet, so it will not run until you add one"

    def test_a_disabled_trigger_does_not_count(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Paused")
        _trigger(tenancy.workspace, flow, TriggerType.COMMENT, enabled=False)

        assert describe_triggers(list(flow.triggers.all())).startswith("No trigger yet")

    def test_two_triggers_are_joined(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Two ways in")
        _trigger(tenancy.workspace, flow, TriggerType.WELCOME)
        _trigger(tenancy.workspace, flow, TriggerType.STORY_REPLY)

        sentence = describe_triggers(list(flow.triggers.all()))

        assert sentence == ("When someone messages you for the first time, or when someone replies to your story")

    def test_a_trigger_the_platform_never_delivers_says_so(self, tenancy):
        """The row above says "Live". Without this it reads as a working flow.

        Instagram publishes no follow webhook field, so a follow trigger is
        enabled, bound, valid and permanently silent — see
        apps/channels/providers/instagram.py's ``_follow_event``.
        """
        flow = create_flow(workspace=tenancy.workspace, name="Thanks for the follow")
        _trigger(tenancy.workspace, flow, TriggerType.FOLLOW)

        sentence = describe_triggers(list(flow.triggers.all()))

        assert sentence == (
            "When someone follows you. Instagram does not support new-follower triggers yet, so it will not run."
        )

    def test_the_caveat_does_not_run_on_when_a_flow_has_two_triggers(self, tenancy):
        """As an inline clause it collided with the "or" that joins the two."""
        flow = create_flow(workspace=tenancy.workspace, name="Two ways in")
        _trigger(tenancy.workspace, flow, TriggerType.COMMENT)
        _trigger(tenancy.workspace, flow, TriggerType.FOLLOW)

        assert describe_triggers(list(flow.triggers.all())) == (
            "When someone comments on a post, or when someone follows you. "
            "Instagram does not support new-follower triggers yet, so it will not run."
        )

    def test_it_does_not_flatten_the_capitals_in_a_keyword(self, tenancy):
        """``str.capitalize()`` lower-cases everything after the first letter.

        The sentence is built lower-case and sentence-cased at the end, so a
        workspace that listens for "Black Friday" used to read it back as
        "black friday" on its own flow list.
        """
        flow = create_flow(workspace=tenancy.workspace, name="Sale")
        _trigger(
            tenancy.workspace,
            flow,
            TriggerType.KEYWORD,
            config={"keywords": [{"text": "Black Friday", "mode": "contains"}]},
        )

        assert describe_triggers(list(flow.triggers.all())) == "When someone sends \u201cBlack Friday\u201d"

    def test_a_trigger_that_does_fire_carries_no_caveat(self, tenancy):
        """The warning has to stay rare or it stops being read."""
        flow = create_flow(workspace=tenancy.workspace, name="Story replies")
        _trigger(tenancy.workspace, flow, TriggerType.STORY_REPLY)

        assert describe_triggers(list(flow.triggers.all())) == "When someone replies to your story"

    def test_three_or_more_stop_listing_and_start_counting(self, tenancy):
        """Three "when…" clauses joined by "or" is a paragraph, not a row."""
        flow = create_flow(workspace=tenancy.workspace, name="Many")
        for trigger_type in (TriggerType.WELCOME, TriggerType.FOLLOW, TriggerType.KEYWORD):
            _trigger(tenancy.workspace, flow, trigger_type)

        assert ", and 2 other triggers" in describe_triggers(list(flow.triggers.all()))


@pytest.mark.django_db
class TestTheListPageRendersIt:
    def test_a_flow_with_a_trigger_renders_its_sentence(self, tenancy, client_for):
        """The regression test for the original bug: the field name was wrong,
        every unit above would have passed against a mock, and only rendering
        the real page with a real trigger caught it."""
        flow = create_flow(workspace=tenancy.workspace, name="Price replies")
        _trigger(tenancy.workspace, flow, TriggerType.COMMENT, {"include_keywords": ["price"]})

        body = (
            client_for(tenancy.owner)
            .get(
                f"/w/{tenancy.workspace.pk}/flows/",
            )
            .content.decode()
        )

        assert "When someone comments “price”" in body
