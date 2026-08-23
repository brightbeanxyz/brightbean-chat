"""The inbound persistence pipeline (SPEC §7.1 step 3, contract 6)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels.events import EventPayload, EventType
from apps.channels.policy import policy_for
from apps.common.platforms import Platform
from apps.contacts.models import Contact
from apps.messaging.ingest import (
    MAX_ATTACHMENTS,
    MAX_MEDIA_ID_CHARS,
    MAX_MEDIA_IDS,
    PERSISTENCE_PROCESSOR,
    ROUTING_PROCESSOR,
    persist_events,
)
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    ConversationState,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    OptInSource,
)
from apps.messaging.tests.conftest import make_connection, make_event

pytestmark = pytest.mark.django_db


def messages(workspace: Any) -> Any:
    return Message.objects.for_workspace(workspace)


class TestSeamRegistration:
    def test_it_registers_persistence_before_routing(self) -> None:
        """Dispatch order is registration order, and routing has to see what
        persistence wrote (contract 6)."""
        names = channels_ingest.registered_processors()
        assert names.index(PERSISTENCE_PROCESSOR) < names.index(ROUTING_PROCESSOR)

    def test_the_routing_tail_is_a_registered_no_op(self, connection: Any) -> None:
        """L4-A takes the stage over by registering under the same name, which
        replaces rather than stacks — so nothing here changes when it lands."""
        assert ROUTING_PROCESSOR in channels_ingest.registered_processors()
        replaced: list[str] = []
        channels_ingest.register_processor(lambda c, e: replaced.append("routed"), name=ROUTING_PROCESSOR)
        channels_ingest.process_events(connection, [make_event(connection)])
        assert replaced == ["routed"]
        assert channels_ingest.registered_processors().count(ROUTING_PROCESSOR) == 1


class TestPersistence:
    def test_one_event_creates_the_whole_chain(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, text="hi")])

        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        message = messages(tenancy.workspace).get()

        assert identity.platform_user_id == "u1"
        assert identity.platform == connection.platform
        assert conversation.contact_id == identity.contact_id
        assert message.direction == MessageDirection.IN
        assert message.body["blocks"] == [{"type": "text", "text": "hi"}]
        assert message.source == ""  # an outbound vocabulary; direction says the rest

    def test_recency_is_stamped_across_all_three_rows(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        contact = Contact.objects.for_workspace(tenancy.workspace).get()
        assert identity.last_inbound_at is not None
        assert contact.last_interaction_at is not None
        assert conversation.last_message_at is not None

    def test_a_second_event_reuses_the_identity_and_conversation(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e1")])
        persist_events(connection, [make_event(connection, event_id="e2")])
        assert ContactChannelIdentity.objects.for_workspace(tenancy.workspace).count() == 1
        assert Conversation.objects.for_workspace(tenancy.workspace).count() == 1
        assert messages(tenancy.workspace).count() == 2

    def test_an_event_with_no_platform_user_id_is_ignored(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, user="")])
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()

    def test_one_bad_event_does_not_cost_its_siblings(self, tenancy: Any, connection: Any) -> None:
        """The seam isolates processors from each other; this isolates events
        from each other inside one. A Meta batch carries unrelated people."""
        broken = make_event(connection, user="u-bad", event_id="e-bad")
        object.__setattr__(broken, "type", "not-an-event-type")
        persist_events(connection, [broken, make_event(connection, user="u-ok", event_id="e-ok")])
        assert messages(tenancy.workspace).count() == 1

    def test_a_processor_failure_never_reaches_the_endpoint(self, tenancy: Any, connection: Any) -> None:
        """SPEC §7.1: never a 5xx for a business-logic failure. persist_events
        swallows and logs, so process_events still reports success."""
        broken = make_event(connection, user="u1")
        object.__setattr__(broken, "connection", None)
        assert channels_ingest.process_events(connection, [broken]) is True


class TestIdempotency:
    def test_double_processing_one_event_writes_one_message(self, tenancy: Any, connection: Any) -> None:
        """The acceptance criterion. webhook_event_log dedups deliveries one
        layer up; this is the line that holds when the seam is driven directly —
        a replayed batch, a retried processor, L4-A's stages re-running."""
        event = make_event(connection, event_id="dup-1")
        persist_events(connection, [event])
        persist_events(connection, [event])
        persist_events(connection, [event])
        assert messages(tenancy.workspace).count() == 1

    def test_the_key_is_the_provider_event_id(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="abc")])
        assert messages(tenancy.workspace).get().idempotency_key == "in:abc"

    def test_two_connections_may_share_a_provider_event_id(self, tenancy: Any, connection: Any) -> None:
        """The key is unique per conversation, and a second connection is a
        second conversation. Provider ids collide across providers."""
        other = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="sms")
        persist_events(connection, [make_event(connection, event_id="e1")])
        persist_events(other, [make_event(other, user="+15550101234", event_id="e1")])
        assert messages(tenancy.workspace).count() == 2


class TestInboundKeyBounds:
    def test_two_long_ids_sharing_a_prefix_are_two_messages(self, tenancy: Any, connection: Any) -> None:
        """Slicing the key to fit the column meant two genuinely different
        messages produced one key, and the second was discarded as a duplicate
        of the first. The id is hashed instead."""
        prefix = "e" * 300
        persist_events(connection, [make_event(connection, event_id=prefix + "a", text="first")])
        persist_events(connection, [make_event(connection, event_id=prefix + "b", text="second")])
        assert messages(tenancy.workspace).count() == 2

    def test_a_long_id_still_deduplicates_itself(self, tenancy: Any, connection: Any) -> None:
        event = make_event(connection, event_id="e" * 400)
        persist_events(connection, [event])
        persist_events(connection, [event])
        assert messages(tenancy.workspace).count() == 1

    def test_a_nul_in_the_event_id_does_not_break_persistence(self, tenancy: Any, connection: Any) -> None:
        """Postgres cannot store NUL in a text column, so an unscrubbed id made
        the insert fail and the message vanish."""
        persist_events(connection, [make_event(connection, event_id="e\x001", text="kept")])
        assert messages(tenancy.workspace).get().body["blocks"][0]["text"] == "kept"

    def test_the_key_fits_the_column(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e" * 5000)])
        assert len(messages(tenancy.workspace).get().idempotency_key) <= 255


class TestReopeningDoneThreads:
    def test_an_inbound_message_reopens_a_finished_thread(self, tenancy: Any, connection: Any) -> None:
        """The inbox list filters on state (SPEC §14), so leaving it done files
        a message the contact just sent somewhere no agent is looking."""
        persist_events(connection, [make_event(connection, event_id="e1")])
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        Conversation.all_objects.filter(pk=conversation.pk).update(state=ConversationState.DONE)

        persist_events(connection, [make_event(connection, event_id="e2")])

        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN

    def test_an_open_thread_is_left_alone(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e1")])
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        assert conversation.state == ConversationState.OPEN


class TestWindowBookkeeping:
    """SPEC §8: written on every inbound event in the webhook path, nowhere else."""

    def test_a_windowed_platform_gets_a_window(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, platform=Platform.INSTAGRAM, suffix="ig")
        before = timezone.now()
        persist_events(connection, [make_event(connection)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        window_hours = policy_for(Platform.INSTAGRAM).window_hours
        assert window_hours is not None
        expected = before + timedelta(hours=window_hours)
        assert identity.window_expires_at is not None
        assert abs((identity.window_expires_at - expected).total_seconds()) < 5

    @pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.SMS, Platform.EMAIL])
    def test_a_windowless_platform_gets_none(self, tenancy: Any, platform: str) -> None:
        """policy.has_window() is consulted first; outside_window is never read
        for these, which is what apps.channels.policy's docstring requires."""
        connection = make_connection(tenancy.workspace, platform=platform, suffix=f"w-{platform}")
        user = "+15550101234" if platform == Platform.SMS else "a@b.com" if platform == Platform.EMAIL else "u1"
        persist_events(connection, [make_event(connection, user=user)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is None

    def test_the_window_is_measured_by_our_clock_not_the_platforms(self, tenancy: Any) -> None:
        """A platform timestamp arrives inside a signed-but-not-trusted payload.
        Letting it set the window means a forged future timestamp buys an
        arbitrarily long right to send."""
        connection = make_connection(tenancy.workspace, platform=Platform.INSTAGRAM, suffix="ig2")
        forged = datetime.now(UTC) + timedelta(days=3650)
        persist_events(connection, [make_event(connection, timestamp=forged)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is not None
        assert identity.window_expires_at < timezone.now() + timedelta(days=2)

    def test_a_later_event_extends_the_window(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, platform=Platform.WHATSAPP, suffix="wa")
        persist_events(connection, [make_event(connection, user="+15550101234", event_id="e1")])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        ContactChannelIdentity.all_objects.filter(pk=identity.pk).update(
            window_expires_at=timezone.now() - timedelta(hours=1)
        )
        persist_events(connection, [make_event(connection, user="+15550101234", event_id="e2")])
        identity.refresh_from_db()
        assert identity.window_expires_at is not None
        assert identity.window_expires_at > timezone.now()


class TestConsentAudit:
    """SPEC §11.8: every identity-creating path records opt_in_at and opt_in_source."""

    def test_an_inbound_message_records_consent(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opt_in is True
        assert identity.opt_in_at is not None
        assert identity.opt_in_source == OptInSource.MESSAGE_IN

    def test_opt_in_at_records_when_consent_was_given_not_last_exercised(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e1")])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        first = identity.opt_in_at
        persist_events(connection, [make_event(connection, event_id="e2")])
        identity.refresh_from_db()
        assert identity.opt_in_at == first

    def test_an_opt_out_event_blocks_the_identity(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e1")])
        persist_events(connection, [make_event(connection, event_id="e2", kind=EventType.OPT_OUT)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False
        assert messages(tenancy.workspace).count() == 1  # the opt-out is not thread content

    def test_messaging_again_is_not_re_consent(self, tenancy: Any, connection: Any) -> None:
        """A contact who sent STOP and then types again has not re-subscribed.
        Treating a message as re-consent is what makes an opt-out look optional;
        re-subscription is an explicit keyword (L5-D's hard_optout hook)."""
        persist_events(connection, [make_event(connection, event_id="e1", kind=EventType.OPT_OUT)])
        persist_events(connection, [make_event(connection, event_id="e2", text="hello again")])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False

    def test_opting_out_twice_keeps_the_first_refusal(self, tenancy: Any, connection: Any) -> None:
        persist_events(connection, [make_event(connection, event_id="e1", kind=EventType.OPT_OUT)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        first = identity.opted_out_at
        persist_events(connection, [make_event(connection, event_id="e2", kind=EventType.OPT_OUT)])
        identity.refresh_from_db()
        assert identity.opted_out_at == first


class TestEventTypes:
    @pytest.mark.parametrize("kind", [EventType.MESSAGE, EventType.POSTBACK, EventType.STORY_REPLY])
    def test_thread_events_become_messages(self, tenancy: Any, connection: Any, kind: str) -> None:
        persist_events(connection, [make_event(connection, kind=kind)])
        assert messages(tenancy.workspace).count() == 1

    @pytest.mark.parametrize("kind", [EventType.STORY_MENTION, EventType.REFERRAL])
    def test_activity_events_open_the_window_without_a_message(self, tenancy: Any, kind: str) -> None:
        """A story mention or an m.me referral is the contact opening a
        conversation, so it opens the window — SPEC §10's Ref URL and Welcome
        triggers depend on that. It is still not a message in the thread."""
        connection = make_connection(tenancy.workspace, platform=Platform.MESSENGER, suffix=f"m-{kind}")
        persist_events(connection, [make_event(connection, kind=kind)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is not None
        assert identity.opt_in is True
        assert messages(tenancy.workspace).count() == 0

    def test_an_activity_event_leaves_last_message_at_alone(self, tenancy: Any) -> None:
        """The inbox sorts on it, so an empty thread must not float to the top."""
        connection = make_connection(tenancy.workspace, platform=Platform.MESSENGER, suffix="m-sort")
        persist_events(connection, [make_event(connection, kind=EventType.REFERRAL)])
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        assert conversation.last_message_at is None

    def test_a_follow_creates_an_identity_but_claims_no_consent(self, tenancy: Any) -> None:
        """Following a page is a relationship, not a message. L5-A's follow
        trigger needs something to match; compliance must still refuse to send,
        so there is no consent and no window."""
        connection = make_connection(tenancy.workspace, platform=Platform.INSTAGRAM, suffix="ig-follow")
        persist_events(connection, [make_event(connection, kind=EventType.FOLLOW)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opt_in is False
        assert identity.opt_in_at is None
        assert identity.window_expires_at is None
        assert not Conversation.objects.for_workspace(tenancy.workspace).exists()

    def test_a_comment_writes_nothing_at_all(self, tenancy: Any) -> None:
        """A comment is public, not a DM. Creating a contact per comment turns
        one viral post into a contact-spam amplifier, and L4-A owns the comment
        infrastructure — when comment-to-DM needs an identity, it is the DM
        that creates one."""
        connection = make_connection(tenancy.workspace, platform=Platform.MESSENGER, suffix="m-comment")
        persist_events(connection, [make_event(connection, kind=EventType.COMMENT)])
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()
        assert not Contact.objects.for_workspace(tenancy.workspace).exists()
        assert messages(tenancy.workspace).count() == 0


class TestBodyShape:
    def test_the_body_mirrors_the_outbound_schema(self, tenancy: Any, connection: Any) -> None:
        """One shape, both directions, so one renderer serves both (SPEC §7.2)."""
        persist_events(connection, [make_event(connection, text="hi")])
        body = messages(tenancy.workspace).get().body
        assert set(body) >= {"blocks", "buttons", "quick_replies", "tag", "template_ref"}

    def test_attachments_are_recorded_but_never_fetched(self, tenancy: Any, connection: Any) -> None:
        """``attachments`` is the field for media a reader's own browser can
        reach without a credential of ours, so there is nothing here for this
        deployment to fetch — see ``EventPayload`` on where the line falls."""
        payload = EventPayload(text="", attachments=("https://cdn.example.test/a.jpg",))
        persist_events(connection, [make_event(connection, payload=payload)])
        body = messages(tenancy.workspace).get().body
        assert body["blocks"] == [{"type": "file", "url": "https://cdn.example.test/a.jpg", "caption": ""}]

    def test_media_ids_reach_the_body_as_identifiers(self, tenancy: Any, connection: Any) -> None:
        """The other side of that line, and the bug this change exists to fix:
        the body used to read ``attachments`` only, so a picture message
        arrived as its caption and nothing else."""
        payload = EventPayload(text="look", media_ids=("AgACAgQAAx0", "AgACAgQAAx1"))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["blocks"] == [
            {"type": "text", "text": "look"},
            {"type": "media", "media_id": "AgACAgQAAx0", "media_kind": "", "caption": ""},
            {"type": "media", "media_id": "AgACAgQAAx1", "media_kind": "", "caption": ""},
        ]

    def test_a_media_id_is_never_written_as_a_url(self, tenancy: Any, connection: Any) -> None:
        """Twilio's identifier *is* a URL, and it is still not one a reader may
        be handed: rendering it as a link is the leak this field exists to
        avoid."""
        url = "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/MM1/Media/ME1"
        persist_events(connection, [make_event(connection, payload=EventPayload(media_ids=(url,)))])
        block = messages(tenancy.workspace).get().body["blocks"][0]
        assert block == {"type": "media", "media_id": url, "media_kind": "", "caption": ""}
        assert "url" not in block

    def test_both_kinds_of_media_can_arrive_together(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(attachments=("https://cdn.example.test/a.jpg",), media_ids=("AgAC",))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert [block["type"] for block in messages(tenancy.workspace).get().body["blocks"]] == ["file", "media"]

    def test_a_pressed_button_is_recorded_for_the_flow_engine(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(text="", button_id="btn:yes")
        persist_events(connection, [make_event(connection, kind=EventType.POSTBACK, payload=payload)])
        assert messages(tenancy.workspace).get().body["button_id"] == "btn:yes"

    def test_a_ref_string_survives_for_the_ref_url_trigger(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(text="/start", ref="promo-42")
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["ref"] == "promo-42"


class TestMalformedPayloadFields:
    @pytest.mark.parametrize("value", [42, None, {"a": 1}, object()])
    def test_a_wrongly_typed_attachments_field_keeps_the_message(
        self, tenancy: Any, connection: Any, value: Any
    ) -> None:
        """EventPayload's contract is that a wrongly typed key leaves the field
        at its default. Slicing this one used to raise TypeError, which
        persist_events swallows — so one bad field cost the whole message rather
        than just its attachments."""
        payload = EventPayload(text="still readable", attachments=value)  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        body = messages(tenancy.workspace).get().body
        assert body["blocks"] == [{"type": "text", "text": "still readable"}]

    def test_a_string_attachments_field_is_not_iterated_as_characters(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(text="", attachments="https://x.test/a.png")  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["blocks"] == []

    @pytest.mark.parametrize("value", [42, None, {"a": 1}, object()])
    def test_a_wrongly_typed_media_ids_field_keeps_the_message(self, tenancy: Any, connection: Any, value: Any) -> None:
        payload = EventPayload(text="still readable", media_ids=value)  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        body = messages(tenancy.workspace).get().body
        assert body["blocks"] == [{"type": "text", "text": "still readable"}]

    def test_a_string_media_ids_field_is_not_iterated_as_characters(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(text="", media_ids="AgAC")  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["blocks"] == []

    def test_media_ids_are_bounded_in_count_and_length(self, tenancy: Any, connection: Any) -> None:
        """SECURITY-BASELINE §7: ``body`` is jsonb, so an unbounded string in it
        is an unbounded row."""
        payload = EventPayload(media_ids=tuple(f"id-{n}" + "x" * 5000 for n in range(MAX_MEDIA_IDS + 5)))
        persist_events(connection, [make_event(connection, payload=payload)])
        blocks = messages(tenancy.workspace).get().body["blocks"]
        assert len(blocks) == MAX_MEDIA_IDS
        assert all(len(block["media_id"]) == MAX_MEDIA_ID_CHARS for block in blocks)


class TestDeliveryStatus:
    @pytest.fixture
    def sent(self, tenancy: Any, connection: Any) -> Message:
        persist_events(connection, [make_event(connection)])
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        return Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            status=MessageStatus.SENT,
            provider_message_id="pm-1",
        )

    def receipt(self, connection: Any, **extra: Any) -> Any:
        return make_event(
            connection,
            kind=EventType.DELIVERY_STATUS,
            event_id=f"ds-{extra.get('status', 'x')}",
            payload=EventPayload(extra=extra),
        )

    def test_it_advances_a_message(self, connection: Any, sent: Message) -> None:
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="delivered")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.DELIVERED

    def test_it_never_moves_a_message_backwards(self, connection: Any, sent: Message) -> None:
        """Platforms do not promise receipt ordering, and a late "sent" must not
        un-read a message the agent can see was read."""
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="read")])
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="sent")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.READ

    def test_a_failure_receipt_records_a_code(self, connection: Any, sent: Message) -> None:
        persist_events(
            connection,
            [self.receipt(connection, provider_message_id="pm-1", status="failed", error="unreachable")],
        )
        sent.refresh_from_db()
        assert sent.status == MessageStatus.FAILED
        assert sent.error == "unreachable"

    def test_a_delivery_receipt_beats_an_earlier_failure(self, connection: Any, sent: Message) -> None:
        """Arriving is stronger evidence than a send-time error, and PR 2's
        retry path must not re-send something that actually landed."""
        Message.all_objects.filter(pk=sent.pk).update(status=MessageStatus.FAILED, error="timeout")
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="delivered")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.DELIVERED
        assert sent.error == ""

    def test_a_stale_failure_receipt_cannot_undo_a_delivery(self, connection: Any, sent: Message) -> None:
        """A "failed" for a message the platform already reported delivered is a
        retransmission; acting on it would tell an operator that a delivered
        message failed."""
        Message.all_objects.filter(pk=sent.pk).update(status=MessageStatus.DELIVERED)
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="failed")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.DELIVERED

    def test_an_unknown_provider_id_is_ignored(self, tenancy: Any, connection: Any, sent: Message) -> None:
        """A receipt for a message this deployment never sent is normal — a
        shared page, a restored backup — not an error."""
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-nope", status="read")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.SENT

    def test_a_queued_receipt_cannot_walk_a_failed_message_backwards(
        self, tenancy: Any, connection: Any, sent: Message
    ) -> None:
        """It used to: 'queued' passed the vocabulary check, and the
        beats-a-failure rule then wrote it over the failure and cleared the
        error — leaving a row in a state nothing would ever move again."""
        Message.all_objects.filter(pk=sent.pk).update(status=MessageStatus.FAILED, error="timeout")
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="queued")])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.FAILED
        assert sent.error == "timeout"

    def test_a_receipt_never_touches_the_window(self, tenancy: Any, connection: Any, sent: Message) -> None:
        """Our own delivery receipt is not the contact interacting with us."""
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        before = identity.last_inbound_at
        persist_events(connection, [self.receipt(connection, provider_message_id="pm-1", status="delivered")])
        identity.refresh_from_db()
        assert identity.last_inbound_at == before

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"provider_message_id": "pm-1"},
            {"status": "delivered"},
            {"provider_message_id": "pm-1", "status": "not-a-status"},
            {"provider_message_id": "", "status": "read"},
            # "queued" is a real MessageStatus but not a receipt: it is a state
            # *we* put a message into before calling anyone, never something a
            # platform reports back.
            {"provider_message_id": "pm-1", "status": "queued"},
        ],
    )
    def test_an_unusable_payload_is_ignored_rather_than_raised_on(
        self, connection: Any, sent: Message, extra: dict[str, Any]
    ) -> None:
        persist_events(connection, [self.receipt(connection, **extra)])
        sent.refresh_from_db()
        assert sent.status == MessageStatus.SENT


class TestTheDeclaredMediaKind:
    """``media_kinds`` is positionally aligned with ``media_ids`` by the adapter.

    Which means it is aligned by *code we wrote*, not by the platform — so the
    failure mode to defend against is our own bug, and the cost of getting it
    wrong must be a mislabelled attachment rather than a lost message
    (``persist_events`` swallows, so an IndexError here drops the whole row).
    """

    def test_a_declared_kind_reaches_the_block(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(media_ids=("a", "b"), media_kinds=("image", "audio"))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert [b["media_kind"] for b in messages(tenancy.workspace).get().body["blocks"]] == ["image", "audio"]

    def test_a_short_kinds_tuple_leaves_the_rest_unknown(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(media_ids=("a", "b", "c"), media_kinds=("image",))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert [b["media_kind"] for b in messages(tenancy.workspace).get().body["blocks"]] == ["image", "", ""]

    def test_a_longer_kinds_tuple_is_simply_ignored(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(media_ids=("a",), media_kinds=("image", "audio", "video"))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert [b["media_kind"] for b in messages(tenancy.workspace).get().body["blocks"]] == ["image"]

    def test_an_adapter_that_fills_nothing_still_works(self, tenancy: Any, connection: Any) -> None:
        """The field defaults to empty so an existing adapter needs no change."""
        payload = EventPayload(media_ids=("a",))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["blocks"][0]["media_kind"] == ""

    @pytest.mark.parametrize("kinds", [42, None, {"a": 1}, "image", object()])
    def test_a_wrongly_typed_kinds_field_keeps_the_message(self, tenancy: Any, connection: Any, kinds: Any) -> None:
        payload = EventPayload(text="still readable", media_ids=("a",), media_kinds=kinds)  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        blocks = messages(tenancy.workspace).get().body["blocks"]
        assert blocks[1]["media_kind"] == ""

    @pytest.mark.parametrize("kind", ["sticker", "IMAGE", "<script>", "", 42, None])
    def test_a_kind_outside_the_vocabulary_is_stored_as_unknown(self, tenancy: Any, connection: Any, kind: Any) -> None:
        """The value reaches the renderer's tag choice and arrived over a
        webhook, so it is an allowlist rather than a passthrough."""
        payload = EventPayload(media_ids=("a",), media_kinds=(kind,))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert messages(tenancy.workspace).get().body["blocks"][0]["media_kind"] == ""


class TestTheTwoMediaListsAreBoundedSeparately:
    def test_each_list_has_its_own_budget(self, tenancy: Any, connection: Any) -> None:
        """Reusing MAX_ATTACHMENTS for both quietly doubled the row bound it
        expressed (SECURITY-BASELINE §7). Two constants, two budgets, and a
        worst case that can be read off rather than inferred."""
        payload = EventPayload(
            attachments=tuple(f"https://cdn.test/{n}.jpg" for n in range(MAX_ATTACHMENTS + 5)),
            media_ids=tuple(f"id-{n}" for n in range(MAX_MEDIA_IDS + 5)),
        )
        persist_events(connection, [make_event(connection, payload=payload)])

        blocks = messages(tenancy.workspace).get().body["blocks"]
        assert sum(1 for b in blocks if b["type"] == "file") == MAX_ATTACHMENTS
        assert sum(1 for b in blocks if b["type"] == "media") == MAX_MEDIA_IDS
