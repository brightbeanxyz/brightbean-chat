"""Parsing Messenger deliveries — the recorded shapes, one per webhook field.

Issue #18 lists the fields: ``messages``, ``messaging_postbacks``,
``messaging_referrals``, ``message_deliveries``, ``message_reads`` and ``feed``.
There is a recorded payload for each in ``fixtures/messenger/``, and a test here
for each, because a parser tested against payloads we invented is a parser tested
against our own misunderstanding.

The hostile half of SECURITY-BASELINE §2 lives in ``test_messenger_hostile.py``.
"""

import json
from typing import Any

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.messenger import (
    COMMENT_PARENT_ID_KEY,
    COMMENT_POST_ID_KEY,
    MessengerAdapter,
)
from apps.channels.tests.messenger_support import PAGE_ID, PSID, load_delivery
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db


def request_for(payload: Any) -> HttpRequest:
    """A request shaped the way the webhook endpoint hands one to an adapter."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RequestFactory().post("/webhooks/messenger/", data=body, content_type="application/json")


def parse(payload: Any, connection: ChannelConnection) -> list[Any]:
    return MessengerAdapter().parse_events(request_for(payload), connection)


def parse_fixture(name: str, connection: ChannelConnection) -> list[Any]:
    return parse(load_delivery(name), connection)


class TestMessages:
    def test_text(self, page: ChannelConnection) -> None:
        (event,) = parse_fixture("message_text", page)
        assert event.type == EventType.MESSAGE
        assert event.platform_user_id == PSID
        assert event.payload.text == "hello there"
        # The ``mid``, not a content hash: Meta supplies a stable per-message id,
        # so a redelivery of the same message deduplicates (SPEC §7.1 step 2).
        assert event.provider_event_id == "fb:m_AAAAbbbbCCCC1111"
        assert event.payload.extra["page_id"] == PAGE_ID

    def test_an_echo_of_our_own_send_is_dropped(self, page: ChannelConnection) -> None:
        """Subscribing to ``messages`` mirrors our own sends back.

        Ingesting one would file every outbound message as an inbound one — and
        reopen the 24-hour messaging window on our own traffic, which is a
        compliance failure rather than a cosmetic bug.
        """
        assert parse_fixture("message_echo", page) == []

    def test_attachments_are_recorded_as_urls(self, page: ChannelConnection) -> None:
        """Meta sends real URLs, unlike Telegram's opaque ``file_id``.

        So they travel in ``payload.attachments``, which SPEC §7.2 documents as
        URLs — and they are recorded, never fetched (SECURITY-BASELINE §6).
        """
        (event,) = parse_fixture("message_attachment", page)
        assert event.payload.attachments == (
            "https://scontent.example.test/a.jpg",
            "https://example.test/shared",
        )
        assert event.payload.media_ids == ()

    def test_a_message_with_neither_text_nor_attachments_is_dropped(self, page: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        payload["entry"][0]["messaging"][0]["message"] = {"mid": "m_sticker", "sticker_id": 369239263222822}
        assert parse(payload, page) == []

    def test_a_message_with_no_mid_is_dropped(self, page: ChannelConnection) -> None:
        """Without an id there is nothing to deduplicate a redelivery against."""
        payload = load_delivery("message_text")
        del payload["entry"][0]["messaging"][0]["message"]["mid"]
        assert parse(payload, page) == []


class TestQuickReplies:
    def test_a_tapped_chip_is_a_postback(self, page: ChannelConnection) -> None:
        """A quick reply is a button press, and this project calls that a postback.

        Meta delivers it as a *message* carrying ``quick_reply.payload``, which is
        the shape — but treating it as a message would let the chip's own label
        fire a keyword trigger, and would let SPEC §9.3's default reply answer "I
        didn't understand that" to a button the flow itself offered.
        """
        (event,) = parse_fixture("message_quick_reply", page)
        assert event.type == EventType.POSTBACK
        # The button half of ``node_id:button_id``. The engine matches a press on
        # the button id against the waiting node's handles.
        assert event.payload.button_id == "yes"
        assert event.payload.text == "Yes please"
        assert event.provider_event_id == "fb:m_qr1"


class TestPostbacks:
    def test_get_started(self, page: ChannelConnection) -> None:
        """SPEC §10's welcome trigger, with no Messenger-specific matcher.

        ``GET_STARTED`` is already one of the spellings
        ``apps.flows.triggers.matching.WELCOME_POSTBACKS`` recognises, which is
        why the adapter configures exactly that payload at connect time.
        """
        from apps.flows.triggers.matching import WELCOME_POSTBACKS

        (event,) = parse_fixture("postback_get_started", page)
        assert event.type == EventType.POSTBACK
        assert event.payload.button_id in WELCOME_POSTBACKS

    def test_a_flow_button(self, page: ChannelConnection) -> None:
        (event,) = parse_fixture("postback_button", page)
        assert event.payload.button_id == "buy"
        assert event.payload.extra["postback_payload"] == "node-3:buy"

    def test_two_presses_of_one_button_are_two_events(self, page: ChannelConnection) -> None:
        """Meta sends no id for a postback, so one is derived — from the timestamp too.

        Without the timestamp in the hash, somebody pressing the same button twice
        would produce one event and the second press would vanish as a duplicate.
        """
        first = load_delivery("postback_button")
        second = load_delivery("postback_button")
        second["entry"][0]["messaging"][0]["timestamp"] = 1712345699000
        (one,) = parse(first, page)
        (two,) = parse(second, page)
        assert one.provider_event_id != two.provider_event_id

    def test_a_redelivered_postback_is_one_event(self, page: ChannelConnection) -> None:
        (one,) = parse_fixture("postback_button", page)
        (again,) = parse_fixture("postback_button", page)
        assert one.provider_event_id == again.provider_event_id

    def test_a_postback_with_no_payload_produces_nothing(self, page: ChannelConnection) -> None:
        payload = load_delivery("postback_button")
        payload["entry"][0]["messaging"][0]["postback"] = {"title": "Buy now"}
        assert parse(payload, page) == []


class TestReferrals:
    def test_a_standalone_m_me_ref(self, page: ChannelConnection) -> None:
        (event,) = parse_fixture("referral", page)
        assert event.type == EventType.REFERRAL
        assert event.payload.ref == "spring-sale"
        assert event.payload.extra["source"] == "SHORTLINK"

    def test_a_ref_inside_a_get_started_postback_is_both_events(self, page: ChannelConnection) -> None:
        """Somebody opening the conversation from an m.me link is two facts.

        SPEC §6.4 delivers the ref *inside* the get-started postback for a first
        contact. Picking one of the two would lose either the welcome signal or
        the ref the person actually clicked.
        """
        events = parse_fixture("postback_with_referral", page)
        assert [event.type for event in events] == [EventType.POSTBACK, EventType.REFERRAL]
        assert events[1].payload.ref == "spring-sale"
        assert events[0].provider_event_id != events[1].provider_event_id

    def test_a_referral_with_no_ref_reads_as_the_welcome_signal(self, page: ChannelConnection) -> None:
        """``matching._is_welcome`` treats an empty ref as "arrived with no payload"."""
        from apps.flows.triggers.matching import _is_welcome

        payload = load_delivery("referral")
        payload["entry"][0]["messaging"][0]["referral"] = {"source": "SHORTLINK", "type": "OPEN_THREAD"}
        (event,) = parse(payload, page)
        assert event.type == EventType.REFERRAL
        assert event.payload.ref == ""
        assert _is_welcome(event) is True

    def test_a_ref_is_bounded_to_what_a_trigger_can_hold(self, page: ChannelConnection) -> None:
        from apps.channels.providers.messenger import MAX_REF_CHARS

        payload = load_delivery("referral")
        payload["entry"][0]["messaging"][0]["referral"]["ref"] = "r" * 5000
        (event,) = parse(payload, page)
        assert len(event.payload.ref) == MAX_REF_CHARS


class TestComments:
    def test_a_top_level_comment(self, page: ChannelConnection) -> None:
        (event,) = parse_fixture("feed_comment", page)
        assert event.type == EventType.COMMENT
        assert event.payload.comment_id == f"{PAGE_ID}_9001"
        assert event.payload.text == "how much is this?"
        assert event.payload.extra[COMMENT_POST_ID_KEY] == f"{PAGE_ID}_8001"

    def test_meta_calls_a_top_level_comments_parent_the_post(self, page: ChannelConnection) -> None:
        """The trap this parser exists to avoid.

        Meta sets ``parent_id`` to the *post* on a top-level comment and to the
        parent comment on a reply. ``apps.flows.triggers.types`` reads an empty
        parent as "top level", so passing Meta's value straight through would make
        SPEC §10's ``top_level_only`` match nothing at all.
        """
        (top_level,) = parse_fixture("feed_comment", page)
        assert COMMENT_PARENT_ID_KEY not in top_level.payload.extra

        (reply,) = parse_fixture("feed_comment_reply", page)
        assert reply.payload.extra[COMMENT_PARENT_ID_KEY] == f"{PAGE_ID}_9001"

    def test_matching_reads_the_same_keys_this_parser_writes(self, page: ChannelConnection) -> None:
        """The contract in ``apps.flows.triggers.types``, both halves at once."""
        from apps.flows.triggers.matching import MatchContext

        (top_level,) = parse_fixture("feed_comment", page)
        context = MatchContext.from_event(page, top_level)
        assert context.post_id == f"{PAGE_ID}_8001"
        assert context.is_top_level_comment is True

        (reply,) = parse_fixture("feed_comment_reply", page)
        assert MatchContext.from_event(page, reply).is_top_level_comment is False

    def test_the_pages_own_comment_is_never_an_event(self, page: ChannelConnection) -> None:
        """Our public reply arrives back through the same ``feed`` subscription.

        Without this the reply we just posted would match the comment trigger and
        start a flow at ourselves.
        """
        assert parse_fixture("feed_own_comment", page) == []

    def test_a_like_is_not_a_comment(self, page: ChannelConnection) -> None:
        assert parse_fixture("feed_like", page) == []

    def test_an_edited_comment_is_ignored(self, page: ChannelConnection) -> None:
        """Only ``verb: add``. An edit is not a new person asking for something."""
        payload = load_delivery("feed_comment")
        payload["entry"][0]["changes"][0]["value"]["verb"] = "edited"
        assert parse(payload, page) == []


class TestDeliveryReceipts:
    def test_one_event_per_delivered_message_id(self, page: ChannelConnection) -> None:
        events = parse_fixture("delivery", page)
        assert [event.type for event in events] == [EventType.DELIVERY_STATUS] * 2
        assert [event.payload.extra["provider_message_id"] for event in events] == ["mid.out-1", "mid.out-2"]
        assert {event.payload.extra["status"] for event in events} == {"delivered"}

    def test_the_payload_shape_is_the_one_ingest_documents(self, page: ChannelConnection) -> None:
        """``apps.messaging.ingest`` fixes the convention; this is the other half."""
        from apps.messaging.ingest import RECEIPT_STATUSES

        for event in parse_fixture("delivery", page):
            assert set(event.payload.extra) >= {"provider_message_id", "status"}
            assert event.payload.extra["status"] in RECEIPT_STATUSES

    def test_only_so_many_read_receipts_per_delivery_are_resolved(self, page: ChannelConnection) -> None:
        """Resolving a watermark costs queries, inside SPEC §7.1's ack budget.

        A delivery may legally carry up to ``meta_common.MAX_ENTRIES`` entries, so
        without a cap one signed batch could put a couple of hundred queries in
        front of the 200 the platform is waiting for. Real batches carry a
        handful; past the cap the receipts are dropped rather than the delivery
        refused, because a read receipt is bookkeeping and a slow ack is not.
        """
        from apps.channels.providers.messenger import MAX_READ_RECEIPTS_PER_DELIVERY

        one = load_delivery("read")["entry"][0]["messaging"][0]
        payload = load_delivery("read")
        payload["entry"][0]["messaging"] = [
            {**one, "timestamp": 1712345687000 + index} for index in range(MAX_READ_RECEIPTS_PER_DELIVERY * 4)
        ]
        # No outbound messages exist here, so every resolution answers empty —
        # the assertion is about how many were *attempted*, which the budget
        # bounds whether or not any resolve.
        adapter = MessengerAdapter()
        assert adapter.parse_events(request_for(payload), page) == []
        assert adapter._read_receipt_budget == 0

    def test_a_delivery_with_no_mids_produces_nothing(self, page: ChannelConnection) -> None:
        payload = load_delivery("delivery")
        payload["entry"][0]["messaging"][0]["delivery"] = {"watermark": 1712345686000}
        assert parse(payload, page) == []


class TestMultiPageDeliveries:
    def test_each_entry_resolves_its_own_connection(self, tenancy: Any, page: ChannelConnection) -> None:
        """One Meta delivery legitimately spans several pages.

        ``views_webhooks._record`` groups a batch by each event's own connection
        precisely so page B's messages are not logged and dispatched as page A's —
        which on a deployment hosting both is a cross-page misattribution.
        """
        from apps.channels.providers import meta_common

        other = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.MESSENGER.value,
            display_name="Second page",
            external_id="333333333333333",
        )
        meta_common.store_page_token(other, "EAAsecondpagetoken0123456789abcdef")
        other.rotate_webhook_secret()
        other.save()

        payload = load_delivery("message_text")
        second = json.loads(json.dumps(payload["entry"][0]))
        second["id"] = other.external_id
        second["messaging"][0]["message"]["mid"] = "m_second"
        payload["entry"].append(second)

        events = parse(payload, page)
        assert [event.connection.pk for event in events] == [page.pk, other.pk]

    def test_an_entry_for_a_page_we_do_not_hold_is_dropped(self, page: ChannelConnection) -> None:
        """The signature proves the sender holds the app secret, not that every
        id in the body is ours to write."""
        payload = load_delivery("message_text")
        payload["entry"][0]["id"] = "999999999999999"
        assert parse(payload, page) == []

    def test_an_entry_with_no_page_id_belongs_to_the_verified_connection(self, page: ChannelConnection) -> None:
        payload = load_delivery("message_text")
        del payload["entry"][0]["id"]
        (event,) = parse(payload, page)
        assert event.connection.pk == page.pk


class TestObjectFiltering:
    def test_a_non_page_object_produces_nothing(self, page: ChannelConnection) -> None:
        """An Instagram batch that reached the Messenger URL is not close enough."""
        payload = load_delivery("message_text")
        payload["object"] = "instagram"
        assert parse(payload, page) == []

    def test_standby_is_ignored(self, page: ChannelConnection) -> None:
        """The handover protocol: another app owns the thread right now."""
        payload = load_delivery("message_text")
        payload["entry"][0]["standby"] = payload["entry"][0].pop("messaging")
        assert parse(payload, page) == []
