"""Hostile Messenger payloads — SECURITY-BASELINE §2's half of the fixture suite.

    Webhook payload parsing is defensive: type-check every field, tolerate
    missing/extra keys, cap sizes. Fixture suites must include malformed and
    hostile payloads (oversized, wrong types, script/injection strings in every
    string field).

"Every string field" is taken literally: :func:`mutations` walks each recorded
delivery and produces one payload per leaf per hostile value, so the suite grows
by itself when a fixture gains a field.

The contract ``parse_events`` is held to is narrow and absolute: **it never
raises**, whatever arrives, and it never emits a half-populated event. The
endpoint's own catch-all (``views_webhooks._parse_events``) is a backstop for our
bugs, not a licence for the parser to throw — a raise there costs the whole
delivery, including the four good events beside the bad one.

The corpus is ``apps.messaging.tests.hostile``, shared rather than reinvented: a
second, differently-wrong list of attack strings is how one layer ends up testing
something the next does not.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from apps.channels.events import EventPayload, NormalizedEvent
from apps.channels.models import ChannelConnection
from apps.channels.providers.messenger import (
    MAX_INBOUND_TEXT_CHARS,
    MAX_REF_CHARS,
    MessengerAdapter,
)
from apps.channels.tests.messenger_support import PAGE_ID, load_delivery
from apps.channels.tests.test_messenger_inbound import parse, request_for
from apps.messaging.tests.hostile import INJECTIONS, OVERSIZED, WRONG_TYPES

pytestmark = pytest.mark.django_db

#: Every recorded delivery, so the sweep covers each webhook field.
FIXTURES = (
    "message_text",
    "message_attachment",
    "message_quick_reply",
    "postback_button",
    "postback_with_referral",
    "referral",
    "delivery",
    "read",
    "feed_comment",
)

#: The hostile values. Wrong types included: an adapter should never *emit* one,
#: and "should never" is exactly the assumption a defensive layer does not make.
HOSTILE: tuple[Any, ...] = (*INJECTIONS, *OVERSIZED, *WRONG_TYPES)


def _paths(node: Any, prefix: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Every leaf position in a decoded payload, as a path of keys and indices."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, (*prefix, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _paths(value, (*prefix, index))
    else:
        yield prefix


def _replace(payload: Any, path: tuple[Any, ...], value: Any) -> Any:
    """A copy of ``payload`` with the leaf at ``path`` replaced."""
    clone = json.loads(json.dumps(payload))
    node = clone
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value
    return clone


def mutations(name: str) -> Iterator[tuple[str, Any]]:
    """``(description, payload)`` for every leaf of ``name`` × every hostile value."""
    payload = load_delivery(name)
    for path in _paths(payload):
        for value in HOSTILE:
            yield (
                f"{name}:{'.'.join(str(step) for step in path)}={type(value).__name__}",
                _replace(payload, path, value),
            )


class TestNothingRaises:
    @pytest.mark.parametrize("name", FIXTURES)
    def test_every_string_field_survives_every_hostile_value(self, name: str, page: ChannelConnection) -> None:
        """One payload per leaf per attack string. Nothing raises, ever."""
        checked = 0
        for description, payload in mutations(name):
            try:
                events = parse(payload, page)
            except Exception as exc:  # pragma: no cover - the assertion is the point
                pytest.fail(f"parse_events raised on {description}: {exc!r}")
            assert isinstance(events, list), description
            checked += 1
        # A sweep that silently generated nothing would pass without testing
        # anything, which is the failure mode a coverage number does not catch.
        assert checked > 0

    @pytest.mark.parametrize("name", FIXTURES)
    def test_dropping_any_key_never_raises(self, name: str, page: ChannelConnection) -> None:
        """Tolerate missing keys, not just wrongly typed ones."""
        payload = load_delivery(name)
        for path in _paths(payload):
            clone = json.loads(json.dumps(payload))
            node = clone
            for step in path[:-1]:
                node = node[step]
            del node[path[-1]]
            assert isinstance(parse(clone, page), list)

    def test_extra_keys_are_ignored(self, page: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["surprise"] = {"deep": [1, 2, 3]}
        payload["something_new"] = "hello"
        (event,) = parse(payload, page)
        assert event.payload.text == "hello there"


class TestShapesThatAreNotShapes:
    @pytest.mark.parametrize(
        "body",
        [
            b"",
            b"not json at all",
            b"[]",
            b'"a string"',
            b"null",
            b"123",
            json.dumps({"object": "page"}).encode(),
            json.dumps({"object": "page", "entry": "not a list"}).encode(),
            json.dumps({"object": "page", "entry": [None, 1, "two"]}).encode(),
            json.dumps({"object": "page", "entry": [{"id": PAGE_ID, "messaging": {}}]}).encode(),
            json.dumps({"object": "page", "entry": [{"id": PAGE_ID, "changes": [{"field": "feed"}]}]}).encode(),
        ],
    )
    def test_a_body_that_is_not_a_delivery_produces_nothing(self, body: bytes, page: ChannelConnection) -> None:
        assert MessengerAdapter().parse_events(request_for(body), page) == []

    def test_an_absurd_number_of_entries_is_bounded(self, page: ChannelConnection) -> None:
        """A legal body can still be a parse amplifier. ``meta_common.MAX_ENTRIES`` caps it."""
        from apps.channels.providers.meta_common import MAX_ENTRIES

        one = load_delivery("message_text")["entry"][0]
        entries: list[Any] = []
        for index in range(MAX_ENTRIES * 3):
            clone = json.loads(json.dumps(one))
            clone["messaging"][0]["message"]["mid"] = f"m_{index}"
            entries.append(clone)
        assert len(parse({"object": "page", "entry": entries}, page)) == MAX_ENTRIES


class TestBounds:
    """Two layers, and they are not the same layer.

    ``apps.channels.security`` refuses a body over ``WEBHOOK_MAX_BODY_BYTES``
    before anything parses it, so the very largest payloads never reach this
    adapter at all. The caps below are the *second* line: a body well inside that
    limit can still carry a single field far larger than any column wants, and
    bounding it here is what stops us holding the string in the first place
    (SECURITY-BASELINE §§2, 7).
    """

    def test_a_body_over_the_cap_never_reaches_the_parser(self, page: ChannelConnection) -> None:
        from apps.channels import security

        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["text"] = "x" * (security.max_body_bytes() + 1)
        assert parse(payload, page) == []

    def test_inbound_text_is_capped(self, page: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["text"] = "x" * (MAX_INBOUND_TEXT_CHARS * 5)
        (event,) = parse(payload, page)
        assert len(event.payload.text) == MAX_INBOUND_TEXT_CHARS

    def test_a_ref_is_capped(self, page: ChannelConnection) -> None:
        payload = load_delivery("referral")
        payload["entry"][0]["messaging"][0]["referral"]["ref"] = "r" * 50_000
        (event,) = parse(payload, page)
        assert len(event.payload.ref) == MAX_REF_CHARS

    def test_attachments_are_capped_in_count_and_length(self, page: ChannelConnection) -> None:
        from apps.channels.providers.messenger import MAX_ATTACHMENT_URL_CHARS, MAX_ATTACHMENTS

        payload = load_delivery("message_attachment")
        payload["entry"][0]["messaging"][0]["message"]["attachments"] = [
            {"type": "image", "payload": {"url": "https://x.test/" + "a" * 3_000}} for _ in range(MAX_ATTACHMENTS * 5)
        ]
        (event,) = parse(payload, page)
        assert len(event.payload.attachments) == MAX_ATTACHMENTS
        assert all(len(url) <= MAX_ATTACHMENT_URL_CHARS for url in event.payload.attachments)

    def test_an_absurd_sender_id_is_hashed_rather_than_truncated(self, page: ChannelConnection) -> None:
        """The rule every id key in this project follows.

        Truncating narrows an identity key without saying so, and two ids agreeing
        on their first 200 characters would become one person receiving another's
        conversation. Not reachable from a real Meta payload; the point is that it
        cannot become reachable.
        """
        from apps.channels.providers.meta_common import MAX_PLATFORM_ID_CHARS

        first = load_delivery("message_text")
        first["entry"][0]["messaging"][0]["sender"]["id"] = "9" * 400
        second = json.loads(json.dumps(first))
        second["entry"][0]["messaging"][0]["sender"]["id"] = "9" * 399 + "8"
        second["entry"][0]["messaging"][0]["message"]["mid"] = "m_second"

        (one,) = parse(first, page)
        (two,) = parse(second, page)
        assert one.platform_user_id.startswith("sha256:")
        assert len(one.platform_user_id) <= MAX_PLATFORM_ID_CHARS
        assert one.platform_user_id != two.platform_user_id

    def test_a_timestamp_outside_the_platforms_range_does_not_raise(self, page: ChannelConnection) -> None:
        """A wrong clock is cosmetic; refusing the event over it is a lost message."""
        for value in (10**18, -(10**18), float("inf"), float("nan"), "yesterday", True):
            payload = load_delivery("message_text")
            payload["entry"][0]["messaging"][0]["timestamp"] = value
            events = parse(payload, page)
            assert len(events) == 1
            assert events[0].timestamp is not None


class TestAttackerContentSurvivesAsData:
    """Hostile strings are carried, not executed — and never interpreted."""

    @pytest.mark.parametrize("injection", INJECTIONS)
    def test_message_text_is_carried_verbatim(self, injection: str, page: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"]["text"] = injection
        (event,) = parse(payload, page)
        # NUL is the one thing removed, because Postgres holds neither \\x00 in a
        # text column nor \\u0000 in jsonb. Everything else is stored as delivered
        # and escaped at render (SECURITY-BASELINE §2).
        assert event.payload.text == injection.replace("\x00", "")[:MAX_INBOUND_TEXT_CHARS]

    @pytest.mark.parametrize("injection", INJECTIONS)
    def test_a_hostile_comment_body_reaches_the_matcher_as_a_literal(
        self, injection: str, page: ChannelConnection
    ) -> None:
        """SECURITY-BASELINE §3: an SSTI payload in a comment stays a string."""
        from apps.flows.triggers.matching import MatchContext

        payload = load_delivery("feed_comment")
        payload["entry"][0]["changes"][0]["value"]["message"] = injection
        (event,) = parse(payload, page)
        context = MatchContext.from_event(page, event)
        assert "49" not in context.text or "49" in injection


class TestTheParserNeverInventsAnEvent:
    def test_an_event_it_emits_is_always_addressable(self, page: ChannelConnection) -> None:
        """Every emitted event carries an id and an address, or is not emitted.

        ``apps.messaging.ingest`` drops an event with no address and
        ``views_webhooks`` drops one with no ``provider_event_id`` — both loudly,
        both after the delivery has already been logged. A parser that emits a
        half-populated event turns a hostile payload into log noise nobody reads.
        """
        for name in FIXTURES:
            for _description, payload in mutations(name):
                for event in parse(payload, page):
                    assert isinstance(event, NormalizedEvent)
                    assert isinstance(event.payload, EventPayload)
                    assert event.provider_event_id
                    assert event.platform_user_id
                    assert event.connection is not None
