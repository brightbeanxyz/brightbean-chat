"""Parsing Instagram deliveries — the recorded shapes, and the hostile ones.

SECURITY-BASELINE §2 makes both halves mandatory: "webhook payload parsing is
defensive: type-check every field, tolerate missing/extra keys, cap sizes.
Fixture suites must include malformed and hostile payloads (oversized, wrong
types, script/injection strings in every string field)."

The contract ``parse_events`` is held to here is narrow and absolute: **it never
raises**, whatever arrives. One malformed item must not cost a whole delivery,
and the endpoint's own catch-all (``views_webhooks._parse_events``) is a backstop
for our bugs rather than a licence for the parser to throw.
"""

import copy
import json
from typing import Any

import pytest

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.instagram import (
    MAX_INBOUND_TEXT_CHARS,
    MAX_PLATFORM_ID_CHARS,
    InstagramAdapter,
)
from apps.channels.tests.instagram_support import IG_USER_ID, load_delivery, request_for
from apps.flows.triggers.types import COMMENT_PARENT_ID_KEY, COMMENT_POST_ID_KEY

pytestmark = pytest.mark.django_db


def parse(payload: Any, connection: ChannelConnection) -> list[Any]:
    return InstagramAdapter().parse_events(request_for(payload), connection)


class TestRecordedShapes:
    """One recorded delivery per webhook field the issue lists."""

    def test_text_message(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_text"), instagram_connection)
        assert event.type == EventType.MESSAGE
        assert event.platform_user_id == IG_USER_ID
        assert event.payload.text == "Do you ship to Berlin?"
        # The message id, not a content hash: Meta supplies a stable ``mid``, so
        # a redelivery of the same message deduplicates (SPEC §7.1 step 2).
        assert event.provider_event_id == "ig:aWdfZG1fMTox"
        # Recorded so a later message_deletions delivery can find the row.
        assert event.payload.extra["provider_message_id"] == "aWdfZG1fMTox"

    def test_media_attachment(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_attachment"), instagram_connection)
        assert event.type == EventType.MESSAGE
        assert event.payload.attachments == ("https://scontent.cdninstagram.com/v/t51/photo.jpg",)
        assert event.payload.extra["attachment_types"] == ["image"]

    def test_quick_reply_is_a_postback(self, instagram_connection: ChannelConnection) -> None:
        """A quick reply means what a button press means — SPEC §7.2\'s button id."""
        (event,) = parse(load_delivery("quick_reply"), instagram_connection)
        assert event.type == EventType.POSTBACK
        assert event.payload.button_id == "yes"
        # The text is kept too: it is what the contact\'s bubble actually says.
        assert event.payload.text == "Yes please"

    def test_postback(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("postback"), instagram_connection)
        assert event.type == EventType.POSTBACK
        assert event.payload.button_id == "track"
        assert event.provider_event_id == "ig:pb:aWdfZG1fNDox"

    def test_ig_me_referral(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("referral"), instagram_connection)
        assert event.type == EventType.REFERRAL
        assert event.payload.ref == "summer-sale"

    def test_story_mention(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("story_mention"), instagram_connection)
        assert event.type == EventType.STORY_MENTION
        assert event.payload.attachments == ("https://scontent.cdninstagram.com/v/t51/story.jpg",)

    def test_story_reply_carries_its_text_and_story(self, instagram_connection: ChannelConnection) -> None:
        """SPEC §10\'s story-reply trigger takes optional keywords, so text matters."""
        (event,) = parse(load_delivery("story_reply"), instagram_connection)
        assert event.type == EventType.STORY_REPLY
        assert event.payload.text == "love this"
        assert event.payload.extra["story_id"] == "17900000000000000"

    def test_message_deletion(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_deleted"), instagram_connection)
        assert event.type == EventType.MESSAGE_DELETED
        assert event.payload.extra["provider_message_id"] == "aWdfZG1fMTox"
        # A distinct id from the message\'s own, so a delivery carrying both the
        # message and its deletion does not dedup one away as the other.
        assert event.provider_event_id == "ig:del:aWdfZG1fMTox"

    def test_comment(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("comment"), instagram_connection)
        assert event.type == EventType.COMMENT
        assert event.payload.comment_id == "17900000000000101"
        assert event.payload.text == "PRICE please"
        assert event.platform_user_id == IG_USER_ID
        # L4-A\'s published contract for where a comment carries its post.
        assert event.payload.extra[COMMENT_POST_ID_KEY] == "17800000000000200"
        assert COMMENT_PARENT_ID_KEY not in event.payload.extra

    def test_comment_reply_carries_its_parent(self, instagram_connection: ChannelConnection) -> None:
        """Absent means top level, which is what ``top_level_only`` switches on."""
        (event,) = parse(load_delivery("comment_reply"), instagram_connection)
        assert event.payload.extra[COMMENT_PARENT_ID_KEY] == "17900000000000101"

    def test_a_mention_in_a_comment_is_a_comment(self, instagram_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("mention_comment"), instagram_connection)
        assert event.type == EventType.COMMENT
        assert event.payload.comment_id == "17900000000000104"
        assert event.payload.extra["mention"] is True

    def test_a_mention_in_a_caption_is_dropped(self, instagram_connection: ChannelConnection) -> None:
        """It carries no commenter and nothing to reply to."""
        assert parse(load_delivery("mention_caption"), instagram_connection) == []

    def test_follow(self, instagram_connection: ChannelConnection) -> None:
        """Parsed, and in practice never delivered — see the adapter on §10\'s
        "degrade gracefully if the field is unavailable to the app"."""
        (event,) = parse(load_delivery("follow"), instagram_connection)
        assert event.type == EventType.FOLLOW
        assert event.platform_user_id == IG_USER_ID


class TestEchoFiltering:
    def test_our_own_message_is_not_ingested(self, instagram_connection: ChannelConnection) -> None:
        """Otherwise our outbound text is filed as the contact\'s inbound reply,
        matches a keyword trigger, and the bot answers itself."""
        assert parse(load_delivery("echo"), instagram_connection) == []

    def test_our_own_comment_is_not_ingested(self, instagram_connection: ChannelConnection) -> None:
        """The public reply comes straight back as a comment webhook. Acting on
        it would let a comment trigger answer its own reply, forever."""
        assert parse(load_delivery("comment_own"), instagram_connection) == []


class TestWrongObject:
    def test_a_delivery_for_another_platform_is_refused(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["object"] = "page"
        assert parse(payload, instagram_connection) == []

    def test_resolve_connection_refuses_it_too(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["object"] = "page"
        body = json.dumps(payload).encode()
        assert InstagramAdapter().resolve_connection(request_for(payload), body) is None


class TestConnectionResolution:
    def test_resolves_by_account_id(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        body = json.dumps(payload).encode()
        found = InstagramAdapter().resolve_connection(request_for(payload), body)
        assert found is not None
        assert found.pk == instagram_connection.pk

    def test_an_unknown_account_resolves_to_nothing(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["id"] = "17841499999999999"
        body = json.dumps(payload).encode()
        assert InstagramAdapter().resolve_connection(request_for(payload), body) is None

    def test_events_name_their_own_entry_connection(self, tenancy: Any, instagram_connection: Any) -> None:
        """A batch spanning two accounts is normal, and each event says which.

        The framework then decides what to do with the secondary one — it has to
        clear the same gates and belong to the same workspace as the connection
        whose secret verified the delivery (``views_webhooks._event_connection``).
        """
        second = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=instagram_connection.platform,
            display_name="@other",
            external_id="17841400000000002",
        )
        payload = load_delivery("message_text")
        payload["entry"].append(copy.deepcopy(payload["entry"][0]))
        payload["entry"][1]["id"] = second.external_id
        payload["entry"][1]["messaging"][0]["message"]["mid"] = "aWdfZG1fOTox"

        first_event, second_event = parse(payload, instagram_connection)
        assert first_event.connection.pk == instagram_connection.pk
        assert second_event.connection.pk == second.pk

    def test_an_entry_for_an_unknown_account_is_dropped(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["id"] = "17841499999999999"
        assert parse(payload, instagram_connection) == []


# ---------------------------------------------------------------------------
# The hostile set (SECURITY-BASELINE §2)
# ---------------------------------------------------------------------------

#: Strings that have to survive being carried out of a parse and stored, and
#: must never be interpreted by anything. They are asserted to come back
#: verbatim where they are asserted at all: escaping is the renderer\'s job
#: (``apps.inbox.rendering``), and a parser that sanitised here would hide a
#: stored-XSS bug rather than prevent one.
HOSTILE_STRINGS = [
    "<script>alert(1)</script>",
    "{{7*7}}",
    "{% raw %}{% load %}{% endraw %}",
    "'; DROP TABLE messaging_message; --",
    "../../etc/passwd",
    "\x00 embedded nul",
    "https://evil.test/x?a=1&b=<script>",
    "javascript:alert(1)",
    "\n\rfake log line: connection=deadbeef",
    "\u202eeslaf",
]

#: Values that are the wrong *type* wherever a string or an object is expected.
WRONG_TYPES: list[Any] = [None, 0, 1.5, True, False, [], {}, [1, 2, 3], {"a": {"b": {}}}, "", " "]


def _walk(node: Any, replace: Any) -> Any:
    """A copy of ``node`` with every leaf value replaced."""
    if isinstance(node, dict):
        return {key: _walk(value, replace) for key, value in node.items()}
    if isinstance(node, list):
        return [_walk(item, replace) for item in node]
    return replace


ALL_FIXTURES = [
    "message_text",
    "message_attachment",
    "quick_reply",
    "postback",
    "referral",
    "story_mention",
    "story_reply",
    "message_deleted",
    "echo",
    "comment",
    "comment_reply",
    "mention_comment",
    "mention_caption",
    "follow",
]


class TestHostilePayloads:
    """Nothing raises. That is the whole contract, and it is absolute."""

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    @pytest.mark.parametrize("value", WRONG_TYPES)
    def test_every_leaf_replaced_with_the_wrong_type(
        self, name: str, value: Any, instagram_connection: ChannelConnection
    ) -> None:
        parse(_walk(load_delivery(name), value), instagram_connection)

    @pytest.mark.parametrize("name", ALL_FIXTURES)
    @pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
    def test_every_leaf_replaced_with_a_hostile_string(
        self, name: str, hostile: str, instagram_connection: ChannelConnection
    ) -> None:
        parse(_walk(load_delivery(name), hostile), instagram_connection)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"object": "instagram"},
            {"object": "instagram", "entry": None},
            {"object": "instagram", "entry": "not a list"},
            {"object": "instagram", "entry": [None, 1, "x", []]},
            {"object": "instagram", "entry": [{}]},
            {"object": "instagram", "entry": [{"id": None}]},
            {"object": "instagram", "entry": [{"id": [], "messaging": {}}]},
            {"entry": [{"id": "17841400000000001"}]},
            [],
            "a string",
            0,
        ],
    )
    def test_structurally_broken_payloads(self, payload: Any, instagram_connection: ChannelConnection) -> None:
        assert parse(payload, instagram_connection) == []

    def test_a_body_that_is_not_json_at_all(self, instagram_connection: ChannelConnection) -> None:
        assert parse(b"<html>not json</html>", instagram_connection) == []

    def test_hostile_text_survives_verbatim_and_bounded(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["text"] = "<script>alert(1)</script>"
        (event,) = parse(payload, instagram_connection)
        # Stored as delivered. Escaping belongs to the renderer; a parser that
        # sanitised would hide the bug rather than prevent it.
        assert event.payload.text == "<script>alert(1)</script>"

    def test_oversized_text_is_bounded(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["text"] = "x" * 100_000
        (event,) = parse(payload, instagram_connection)
        assert len(event.payload.text) == MAX_INBOUND_TEXT_CHARS

    def test_an_absurd_id_is_hashed_not_truncated(self, instagram_connection: ChannelConnection) -> None:
        """Truncating narrows an identity key without saying so: two ids agreeing
        on their first 200 characters would become one person\'s conversation."""
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["sender"]["id"] = "9" * 5000
        (event,) = parse(payload, instagram_connection)
        assert event.platform_user_id.startswith("sha256:")
        assert len(event.platform_user_id) <= MAX_PLATFORM_ID_CHARS

    def test_two_long_ids_sharing_a_prefix_stay_distinct(self, instagram_connection: ChannelConnection) -> None:
        seen = []
        for tail in ("a", "b"):
            payload = load_delivery("message_text")
            payload["entry"][0]["messaging"][0]["sender"]["id"] = "9" * 300 + tail
            seen.extend(parse(payload, instagram_connection))
        assert seen[0].platform_user_id != seen[1].platform_user_id

    def test_a_nul_only_id_produces_no_event(self, instagram_connection: ChannelConnection) -> None:
        """An id of nothing but NUL scrubs to empty, and an empty key collides
        with every other empty one — the bug ``_dedup_id`` fixed one layer up."""
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["sender"]["id"] = "\x00\x00"
        assert parse(payload, instagram_connection) == []

    def test_an_impossible_timestamp_falls_back_to_now(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["timestamp"] = 10**18
        (event,) = parse(payload, instagram_connection)
        assert event.timestamp is not None

    def test_a_huge_batch_is_bounded(self, instagram_connection: ChannelConnection) -> None:
        """A legal-sized body of tiny entries is thousands of connection lookups."""
        payload = load_delivery("message_text")
        entry = payload["entry"][0]
        payload["entry"] = [copy.deepcopy(entry) for _ in range(500)]
        for index, item in enumerate(payload["entry"]):
            item["messaging"][0]["message"]["mid"] = f"mid-{index}"
        assert len(parse(payload, instagram_connection)) <= 100

    def test_many_messaging_items_in_one_entry_are_bounded(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        item = payload["entry"][0]["messaging"][0]
        payload["entry"][0]["messaging"] = [copy.deepcopy(item) for _ in range(500)]
        for index, one in enumerate(payload["entry"][0]["messaging"]):
            one["message"]["mid"] = f"mid-{index}"
        assert len(parse(payload, instagram_connection)) <= 100

    def test_one_broken_item_does_not_cost_the_others(self, instagram_connection: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"].insert(0, {"sender": None, "message": 12})
        payload["entry"][0]["messaging"].append({"nonsense": True})
        (event,) = parse(payload, instagram_connection)
        assert event.payload.text == "Do you ship to Berlin?"

    def test_every_event_carries_a_usable_dedup_id(self, instagram_connection: ChannelConnection) -> None:
        """No id means every retry is processed again (SPEC §7.1 step 2)."""
        for name in ALL_FIXTURES:
            for event in parse(load_delivery(name), instagram_connection):
                assert event.provider_event_id
                assert "\x00" not in event.provider_event_id
