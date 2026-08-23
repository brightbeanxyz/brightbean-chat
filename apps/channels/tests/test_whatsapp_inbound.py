"""Parsing WhatsApp deliveries — the recorded shapes, and the hostile ones.

SECURITY-BASELINE §2 makes both halves mandatory: "webhook payload parsing is
defensive: type-check every field, tolerate missing/extra keys, cap sizes.
Fixture suites must include malformed and hostile payloads (oversized, wrong
types, script/injection strings in every string field)."

The contract ``parse_events`` is held to here is narrow and absolute: **it never
raises**, whatever arrives. One malformed event must not cost a whole delivery,
and the endpoint's own catch-all (``views_webhooks._parse_events``) is a
backstop for our bugs, not a licence for the parser to throw.
"""

import json
from typing import Any

import pytest
from django.http import HttpRequest
from django.test import RequestFactory

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.whatsapp import (
    MAX_INBOUND_TEXT_CHARS,
    MAX_PLATFORM_ID_CHARS,
    SIGNATURE_HEADER,
    WhatsAppAdapter,
)
from apps.channels.tests.whatsapp_support import (
    APP_SECRET,
    PLATFORM_USER_ID,
    fixture_names,
    load_delivery,
    make_connection,
    signature,
)
from apps.messaging.codes import Denial, Failure

pytestmark = pytest.mark.django_db


def request_for(payload: Any) -> HttpRequest:
    """A request shaped the way the webhook endpoint hands one to an adapter."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = RequestFactory().post("/webhooks/whatsapp/", data=body, content_type="application/json")
    request.META["HTTP_" + SIGNATURE_HEADER.upper().replace("-", "_")] = signature(body, APP_SECRET)
    return request


def parse(payload: Any, connection: ChannelConnection) -> list[Any]:
    return WhatsAppAdapter().parse_events(request_for(payload), connection)


@pytest.fixture
def wa_connection(tenancy: Any) -> ChannelConnection:
    return make_connection(tenancy.workspace)


class TestRecordedShapes:
    """One recorded delivery per inbound shape the issue lists."""

    def test_text(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_text"), wa_connection)
        assert event.type == EventType.MESSAGE
        assert event.payload.text == "Hello there"
        assert event.platform_user_id == PLATFORM_USER_ID
        assert event.provider_event_id == "wa:wamid.TEXT1"
        # The profile name is display detail, not identity.
        assert event.payload.extra["profile_name"] == "Ada Lovelace"

    def test_the_identity_is_e164_with_a_plus(self, wa_connection: ChannelConnection) -> None:
        """The half that lets a WhatsApp identity link to a contact by phone.

        ``normalize_phone`` refuses a bare string of digits — it will not guess a
        country code — so a wa_id stored as Meta sends it would never match
        ``contact.phone``. See ``apps.messaging.identities``.
        """
        from apps.common.addresses import normalize_phone

        (event,) = parse(load_delivery("message_text"), wa_connection)
        assert normalize_phone(event.platform_user_id) == PLATFORM_USER_ID

    def test_image_carries_a_media_id_and_its_caption(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_image"), wa_connection)
        assert event.payload.media_ids == ("MEDIA_IMAGE_1",)
        assert event.payload.text == "My receipt"
        assert event.payload.extra["media_kind"] == "image"

    def test_a_media_id_is_never_reported_as_an_attachment_url(self, wa_connection: ChannelConnection) -> None:
        """``attachments`` is documented as URLs and a media id is not one.

        Turning it into a URL needs a second Graph call and yields a link that
        expires, so the id is carried and resolved on demand — which also keeps
        us clear of SECURITY-BASELINE §6.
        """
        (event,) = parse(load_delivery("message_image"), wa_connection)
        assert event.payload.attachments == ()

    def test_document(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_document"), wa_connection)
        assert event.payload.media_ids == ("MEDIA_DOC_1",)
        assert event.payload.extra["media_kind"] == "file"

    def test_a_voice_note_is_audio(self, wa_connection: ChannelConnection) -> None:
        """Aliases are folded so no consumer has to learn WhatsApp's vocabulary."""
        (event,) = parse(load_delivery("message_audio"), wa_connection)
        assert event.payload.extra["media_kind"] == "audio"

    def test_location_becomes_the_sentence_a_person_would_have_typed(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_location"), wa_connection)
        assert event.payload.text.startswith("Shared a location: 51.5, -0.12")

    def test_a_shared_contact_becomes_text(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("message_contacts"), wa_connection)
        assert "Grace Hopper" in event.payload.text

    def test_a_reply_button_is_a_postback(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("interactive_button_reply"), wa_connection)
        assert event.type == EventType.POSTBACK
        # node_id:button_id, split on the first colon — the id the waiting node's
        # handles are matched against.
        assert event.payload.button_id == "yes"
        assert event.payload.extra["reply_id"] == "n1:yes"

    def test_a_list_row_is_a_postback_too(self, wa_connection: ChannelConnection) -> None:
        """The engine matches on the handle and does not care which widget replied."""
        (event,) = parse(load_delivery("interactive_list_reply"), wa_connection)
        assert event.type == EventType.POSTBACK
        assert event.payload.button_id == "medium"

    def test_a_template_quick_reply_has_no_node_prefix(self, wa_connection: ChannelConnection) -> None:
        """Its payload is authored in Meta's console, so there is no colon to split."""
        (event,) = parse(load_delivery("template_button_reply"), wa_connection)
        assert event.type == EventType.POSTBACK
        assert event.payload.button_id == "track_order"

    def test_a_type_we_do_not_carry_produces_nothing(self, wa_connection: ChannelConnection) -> None:
        assert parse(load_delivery("message_reaction"), wa_connection) == []

    def test_another_subscription_field_is_ignored(self, wa_connection: ChannelConnection) -> None:
        """One subscription delivers account updates and template reviews too."""
        assert parse(load_delivery("account_update"), wa_connection) == []

    def test_another_webhook_object_is_ignored(self, wa_connection: ChannelConnection) -> None:
        assert parse(load_delivery("other_object"), wa_connection) == []


class TestDeliveryStatuses:
    """``statuses[]`` becomes exactly the shape ``messaging.ingest`` consumes."""

    @pytest.mark.parametrize(
        "name,status",
        [("status_sent", "sent"), ("status_delivered", "delivered"), ("status_read", "read")],
    )
    def test_each_state_on_the_ladder(self, wa_connection: ChannelConnection, name: str, status: str) -> None:
        (event,) = parse(load_delivery(name), wa_connection)
        assert event.type == EventType.DELIVERY_STATUS
        assert event.payload.extra == {"provider_message_id": "wamid.OUT1", "status": status}

    def test_three_receipts_for_one_message_are_three_events(self, wa_connection: ChannelConnection) -> None:
        """Namespacing the dedup key by state is what keeps the ladder moving.

        One id per message would file `delivered` and `read` as duplicates of
        `sent`, and the message would never leave the first rung.
        """
        ids = {
            parse(load_delivery(name), wa_connection)[0].provider_event_id
            for name in ("status_sent", "status_delivered", "status_read")
        }
        assert len(ids) == 3

    def test_a_re_engagement_failure_reports_needs_template(self, wa_connection: ChannelConnection) -> None:
        """131047 is the asynchronous form of the refusal ``can_send`` gives.

        Reporting Meta's own prose instead would put an unregistered string on
        ``message.error``, which the inbox renders raw and which routinely
        quotes the request that caused it (SECURITY-BASELINE §5).
        """
        (event,) = parse(load_delivery("status_failed_reengagement"), wa_connection)
        assert event.payload.extra["error"] == Denial.NEEDS_TEMPLATE.value

    def test_an_unmapped_failure_is_still_a_registered_code(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("status_failed_unknown"), wa_connection)
        assert event.payload.extra["error"] == Failure.PROVIDER_REJECTED.value

    def test_metas_prose_never_reaches_the_event_payload(self, wa_connection: ChannelConnection) -> None:
        (event,) = parse(load_delivery("status_failed_reengagement"), wa_connection)
        rendered = json.dumps(event.payload.extra)
        assert "24 hours" not in rendered
        assert "Re-engagement" not in rendered
        # It survives in `raw`, which is the event log an operator can read.
        assert "Re-engagement" in json.dumps(event.raw)

    def test_a_state_that_names_no_rung_is_dropped(self, wa_connection: ChannelConnection) -> None:
        delivery = load_delivery("status_sent")
        delivery["entry"][0]["changes"][0]["value"]["statuses"][0]["status"] = "deleted"
        assert parse(delivery, wa_connection) == []


class TestOneDeliveryManyNumbers:
    def test_a_change_for_another_connection_names_it(self, tenancy: Any) -> None:
        """``NormalizedEvent`` carries its own connection so a batch can span numbers."""
        verified = make_connection(tenancy.workspace)
        other = make_connection(tenancy.workspace, phone_number_id="222222222222222")

        delivery = load_delivery("message_text")
        second = json.loads(json.dumps(delivery["entry"][0]["changes"][0]))
        second["value"]["metadata"]["phone_number_id"] = other.external_id
        second["value"]["messages"][0]["id"] = "wamid.TEXT2"
        delivery["entry"][0]["changes"].append(second)

        events = parse(delivery, verified)
        assert [event.connection.pk for event in events] == [verified.pk, other.pk]

    def test_an_unconnected_number_is_dropped(self, wa_connection: ChannelConnection) -> None:
        delivery = load_delivery("message_text")
        delivery["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999999999999999"
        assert parse(delivery, wa_connection) == []


class TestHostilePayloads:
    """Nothing here may raise, and nothing may produce a half-populated event.

    The mutations are generated from the *recorded* shapes rather than written
    out, so every field of every real payload is covered rather than the handful
    somebody thought of.
    """

    @pytest.mark.parametrize("name", fixture_names())
    def test_every_recorded_shape_survives_having_a_field_replaced(
        self, wa_connection: ChannelConnection, name: str
    ) -> None:
        for mutated in _mutations(load_delivery(name)):
            # The claim is only "does not raise"; what it produces is the
            # recorded-shape tests' business.
            WhatsAppAdapter().parse_events(request_for(mutated), wa_connection)

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"not json at all",
            b"[]",
            b'{"object": "whatsapp_business_account"}',
            b'{"object": "whatsapp_business_account", "entry": "not-a-list"}',
            b'{"object": "whatsapp_business_account", "entry": [null, 1, "two"]}',
            b'{"object": null, "entry": []}',
        ],
        ids=["empty", "garbage", "list", "no-entry", "entry-scalar", "entry-of-junk", "null-object"],
    )
    def test_a_body_that_is_not_a_delivery_produces_nothing(
        self, wa_connection: ChannelConnection, payload: bytes
    ) -> None:
        assert parse(payload, wa_connection) == []

    def test_script_in_every_string_field_is_carried_as_text_not_executed(
        self, wa_connection: ChannelConnection
    ) -> None:
        """Attacker-controlled and stored verbatim; escaping happens on render."""
        hostile = "<script>alert(1)</script>"
        delivery = load_delivery("message_text")
        value = delivery["entry"][0]["changes"][0]["value"]
        value["messages"][0]["text"]["body"] = hostile
        value["contacts"][0]["profile"]["name"] = hostile

        (event,) = parse(delivery, wa_connection)
        assert event.payload.text == hostile
        assert event.payload.extra["profile_name"] == hostile

    def test_an_oversized_body_is_bounded_before_it_is_carried(self, wa_connection: ChannelConnection) -> None:
        delivery = load_delivery("message_text")
        delivery["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "x" * 200_000
        (event,) = parse(delivery, wa_connection)
        assert len(event.payload.text) == MAX_INBOUND_TEXT_CHARS

    def test_an_absurd_wa_id_is_hashed_rather_than_truncated(self, wa_connection: ChannelConnection) -> None:
        """Truncating narrows an identity key silently; two long ids agreeing on
        their first 200 characters would become one person's conversation."""
        delivery = load_delivery("message_text")
        value = delivery["entry"][0]["changes"][0]["value"]
        value["messages"][0]["from"] = "9" * 500
        value["contacts"][0]["wa_id"] = "9" * 500

        (event,) = parse(delivery, wa_connection)
        assert event.platform_user_id.startswith("sha256:")
        assert len(event.platform_user_id) <= MAX_PLATFORM_ID_CHARS

    def test_a_wa_id_that_is_not_a_number_is_refused(self, wa_connection: ChannelConnection) -> None:
        """A `+` prefix on arbitrary text would be an address that is not one."""
        delivery = load_delivery("message_text")
        delivery["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "not-a-number"
        assert parse(delivery, wa_connection) == []

    def test_a_message_with_no_id_is_dropped(self, wa_connection: ChannelConnection) -> None:
        """Without one there is no deduplication key, so a retry would be reprocessed."""
        delivery = load_delivery("message_text")
        del delivery["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
        assert parse(delivery, wa_connection) == []

    def test_a_malformed_timestamp_falls_back_to_now_rather_than_dropping(
        self, wa_connection: ChannelConnection
    ) -> None:
        """A wrong clock is cosmetic; a dropped message is not."""
        delivery = load_delivery("message_text")
        delivery["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = "not-a-time"
        (event,) = parse(delivery, wa_connection)
        assert event.timestamp is not None


def _mutations(delivery: Any) -> list[Any]:
    """``delivery`` with each leaf replaced by something of the wrong type.

    Generated rather than written out: a hand-written hostile fixture covers the
    fields its author thought of, and the ones that matter are the others. The
    NUL string is here because Postgres stores neither a NUL in a text column
    nor its escape in jsonb, and a payload carrying one used to be a 500 on the
    one endpoint strangers can reach.
    """
    hostiles: list[Any] = [None, 0, True, [], {}, "<script>alert(1)</script>", "a\x00b", "x" * 10_000]
    out: list[Any] = []
    for hostile in hostiles:
        for path in _leaf_paths(delivery):
            out.append(_replaced(delivery, path, hostile))
    return out


def _leaf_paths(node: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(node, dict):
        return [p for key, value in node.items() for p in _leaf_paths(value, (*prefix, key))]
    if isinstance(node, list):
        return [p for index, value in enumerate(node) for p in _leaf_paths(value, (*prefix, index))]
    return [prefix]


def _replaced(document: Any, path: tuple[Any, ...], value: Any) -> Any:
    clone = json.loads(json.dumps(document))
    node = clone
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return clone
