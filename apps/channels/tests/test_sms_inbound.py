"""Verifying and parsing Twilio's callbacks — the recorded shapes and the hostile ones.

SECURITY-BASELINE §2 makes both halves mandatory: "webhook payload parsing is
defensive: type-check every field, tolerate missing/extra keys, cap sizes.
Fixture suites must include malformed and hostile payloads (oversized, wrong
types, script/injection strings in every string field)."

The contract ``parse_events`` is held to here is narrow and absolute: **it never
raises**, whatever arrives. One malformed callback must not cost a delivery, and
the endpoint's own catch-all is a backstop for our bugs, not a licence for the
parser to throw.

The signature half is the other security-critical piece, and its own class pins
the arithmetic against Twilio's published example rather than against this
implementation's opinion of itself.
"""

from typing import Any

import pytest
from django.http import HttpRequest
from django.test import RequestFactory, override_settings

from apps.channels.events import EventType
from apps.channels.models import ChannelConnection
from apps.channels.providers.sms import (
    MAX_MEDIA,
    SIGNATURE_HEADER,
    TwilioAdapter,
    keyword,
    sign,
    webhook_url,
)
from apps.channels.tests.sms_support import (
    AUTH_TOKEN,
    CONTACT_NUMBER,
    load_payload,
    public_url,
    signature_for,
    sms_connection,
    webhook_path,
)
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

#: Twilio's own worked example, from its request-validation documentation. The
#: anchor for the whole scheme: if this passes, the concatenation order, the
#: key sort, the HMAC and the base64 are all right, and every other test in the
#: repo can sign with our own helper without that being circular.
TWILIO_DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_DOC_TOKEN = "12345"  # noqa: S105 - from Twilio's public documentation
TWILIO_DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
TWILIO_DOC_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


@pytest.fixture
def connection(tenancy: Tenancy) -> ChannelConnection:
    return sms_connection(tenancy.workspace)


def request_for(
    connection: ChannelConnection,
    params: dict[str, str],
    *,
    signature: str | None = None,
    **meta: Any,
) -> HttpRequest:
    """A request shaped the way the webhook endpoint hands one to the adapter."""
    request = RequestFactory().post(webhook_path(connection), data=params)
    if signature is not None:
        request.META["HTTP_X_TWILIO_SIGNATURE"] = signature
    request.META.update(meta)
    return request


def parse(connection: ChannelConnection, params: dict[str, str]) -> list[Any]:
    return TwilioAdapter().parse_events(request_for(connection, params), connection)


class TestSignature:
    def test_it_matches_twilios_published_example(self) -> None:
        assert sign(TWILIO_DOC_TOKEN, TWILIO_DOC_URL, TWILIO_DOC_PARAMS) == TWILIO_DOC_SIGNATURE

    def test_a_correct_signature_verifies(self, connection: ChannelConnection) -> None:
        params = load_payload("inbound_text")
        request = request_for(connection, params, signature=signature_for(params, url=public_url(connection)))

        assert TwilioAdapter().verify_webhook(request, connection) is True

    def test_a_tampered_body_does_not(self, connection: ChannelConnection) -> None:
        params = load_payload("inbound_text")
        signature = signature_for(params, url=public_url(connection))
        request = request_for(connection, {**params, "Body": "transfer the money"}, signature=signature)

        assert TwilioAdapter().verify_webhook(request, connection) is False

    def test_a_tampered_signature_does_not(self, connection: ChannelConnection) -> None:
        params = load_payload("inbound_text")
        request = request_for(connection, params, signature="AAAAAAAAAAAAAAAAAAAAAAAAAAA=")

        assert TwilioAdapter().verify_webhook(request, connection) is False

    @pytest.mark.parametrize("signature", [None, "", "   ", "not base64 at all", "%%%%"])
    def test_it_fails_closed_on_a_missing_or_malformed_header(
        self, connection: ChannelConnection, signature: str | None
    ) -> None:
        """Every rejection is the same rejection — no oracle for the format."""
        request = request_for(connection, load_payload("inbound_text"), signature=signature)

        assert TwilioAdapter().verify_webhook(request, connection) is False

    def test_a_connection_with_no_token_verifies_nothing(self, tenancy: Tenancy) -> None:
        connection = sms_connection(tenancy.workspace, token="", suffix="-x")
        params = load_payload("inbound_text")
        request = request_for(connection, params, signature=signature_for(params, url=public_url(connection), token=""))

        assert TwilioAdapter().verify_webhook(request, connection) is False

    def test_the_signed_url_is_the_public_one_not_the_requests(self, connection: ChannelConnection) -> None:
        """The proxy case, stated directly.

        Behind a reverse proxy the application sees ``testserver`` while Twilio
        signed the deployment's real address. Signing what ``build_absolute_uri``
        would produce must therefore *fail*, and signing ``APP_URL`` must pass —
        which is what makes ``webhook_url`` and ``verify_webhook`` sharing one
        builder load-bearing rather than tidy.
        """
        params = load_payload("inbound_text")
        as_seen = "http://testserver" + webhook_path(connection)

        assert public_url(connection) != as_seen
        assert public_url(connection) == webhook_url(connection)

        good = request_for(connection, params, signature=signature_for(params, url=public_url(connection)))
        assert TwilioAdapter().verify_webhook(good, connection) is True

    def test_a_forwarded_host_is_ignored_from_an_untrusted_peer(self, connection: ChannelConnection) -> None:
        """The bypass this check exists to prevent.

        ``X-Forwarded-Host`` is attacker-controlled unless a configured proxy set
        it. A caller who could choose the host could choose the string their
        forged signature was computed over, which would make the whole scheme
        decorative. ``TRUSTED_PROXIES`` is empty by default, so this must fail.
        """
        params = load_payload("inbound_text")
        forged_url = "https://attacker.example" + webhook_path(connection)
        request = request_for(
            connection,
            params,
            signature=signature_for(params, url=forged_url),
            HTTP_X_FORWARDED_HOST="attacker.example",
            HTTP_X_FORWARDED_PROTO="https",
            REMOTE_ADDR="203.0.113.9",
        )

        assert TwilioAdapter().verify_webhook(request, connection) is False

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/8"], APP_URL="https://not-the-one-twilio-has")
    def test_a_forwarded_host_is_honoured_from_a_trusted_peer(self, connection: ChannelConnection) -> None:
        """And the case it exists to support: a real proxy in front of a real deployment.

        ``APP_URL`` is deliberately pointed somewhere else here, so the only
        candidate that can match is the proxy-declared one.
        """
        params = load_payload("inbound_text")
        proxied_url = "https://chat.example.com" + webhook_path(connection)
        request = request_for(
            connection,
            params,
            signature=signature_for(params, url=proxied_url),
            HTTP_X_FORWARDED_HOST="chat.example.com",
            HTTP_X_FORWARDED_PROTO="https",
            REMOTE_ADDR="10.1.2.3",
        )

        assert TwilioAdapter().verify_webhook(request, connection) is True

    @override_settings(APP_URL="https://not-the-one-twilio-has")
    def test_the_request_url_is_the_last_resort(self, connection: ChannelConnection) -> None:
        """A deployment with no proxy and an APP_URL that does not match."""
        params = load_payload("inbound_text")
        as_seen = "http://testserver" + webhook_path(connection)
        request = request_for(connection, params, signature=signature_for(params, url=as_seen))

        assert TwilioAdapter().verify_webhook(request, connection) is True

    def test_a_query_string_is_part_of_what_is_signed(self, connection: ChannelConnection) -> None:
        """An operator may paste a URL with parameters; Twilio signs it verbatim."""
        params = load_payload("inbound_text")
        path = webhook_path(connection) + "?tag=main"
        request = RequestFactory().post(path, data=params)
        from django.conf import settings

        request.META["HTTP_X_TWILIO_SIGNATURE"] = signature_for(params, url=settings.APP_URL.rstrip("/") + path)

        assert TwilioAdapter().verify_webhook(request, connection) is True

    @pytest.mark.parametrize("host", ["évil.example", "mail.ｅxample.com", "host_with_underscore\u0131"])
    def test_a_non_ascii_forwarded_host_is_refused(self, connection: ChannelConnection, host: str) -> None:
        """``isalnum()`` alone is true for every Unicode letter and digit, so
        the allowlist read as ASCII-only and was not. Even from a trusted peer,
        a homograph host must not become a candidate URL."""
        params = load_payload("inbound_text")
        forged = f"https://{host}" + webhook_path(connection)

        with override_settings(TRUSTED_PROXIES=["10.0.0.0/8"], APP_URL="https://not-the-one-twilio-has"):
            request = request_for(
                connection,
                params,
                signature=signature_for(params, url=forged),
                HTTP_X_FORWARDED_HOST=host,
                HTTP_X_FORWARDED_PROTO="https",
                REMOTE_ADDR="10.1.2.3",
            )

            assert TwilioAdapter().verify_webhook(request, connection) is False

    def test_the_header_name_is_the_documented_one(self) -> None:
        assert SIGNATURE_HEADER == "X-Twilio-Signature"


class TestInboundMessages:
    def test_a_recorded_text_message(self, connection: ChannelConnection) -> None:
        (event,) = parse(connection, load_payload("inbound_text"))

        assert event.type == EventType.MESSAGE
        assert event.platform_user_id == CONTACT_NUMBER
        assert event.provider_event_id == "SM11111111111111111111111111111111"
        assert event.payload.text == "Hello there"
        assert event.payload.media_ids == ()
        assert event.payload.extra == {"city": "NEW YORK", "state": "NY", "country": "US"}
        assert event.connection == connection

    def test_an_mms_carries_its_media_as_ids_not_attachments(self, connection: ChannelConnection) -> None:
        """``attachments`` is specified as URLs a consumer may use, and a Twilio
        ``MediaUrl`` is not one: it addresses a REST resource under the account,
        so with authenticated media it answers 401 to a browser and without it
        it answers to anyone at all. ``messaging.ingest`` turns ``attachments``
        into ``{"type": "file", "url": …}`` blocks that ``apps.inbox.rendering``
        emits as links, so putting them there would hand out one or the other.
        Same call ``telegram._media_ids`` makes."""
        (event,) = parse(connection, load_payload("inbound_mms"))

        assert event.payload.text == "Look at these"
        assert event.payload.attachments == ()
        assert len(event.payload.media_ids) == 2
        assert all(url.startswith("https://api.twilio.com/") for url in event.payload.media_ids)

    def test_media_never_reaches_the_thread_as_a_browser_link(self, connection: ChannelConnection) -> None:
        """The property that matters downstream, asserted against the real
        serialiser rather than inferred from the field name.

        The claim is "never handed to a browser as a link", not "never
        recorded". This test used to assert the second, because at the time
        ``_inbound_body`` read only ``attachments`` and dropped media ids on the
        floor — the picture was unreachable, which is not the same as safe. Now
        the id is stored as an *identifier*, in a field no renderer turns into
        an href, and resolved through ``apps.channels.media`` against the
        account's own credentials.
        """
        from apps.messaging.ingest import _inbound_body

        (event,) = parse(connection, load_payload("inbound_mms"))
        body = _inbound_body(event)

        assert [block["type"] for block in body["blocks"]] == ["text", "media", "media"]
        # The Twilio URL appears only as an identifier. Every place a renderer
        # looks for something to put in an href or a src — ``url`` on a block,
        # ``image_url`` on a card — is where it must not be.
        media = [block for block in body["blocks"] if block["type"] == "media"]
        assert all(block["media_id"].startswith("https://api.twilio.com/") for block in media)
        assert all("url" not in block for block in body["blocks"])
        assert "api.twilio.com" not in str([block for block in body["blocks"] if block["type"] != "media"])

    def test_media_alone_is_still_a_message(self, connection: ChannelConnection) -> None:
        params = {**load_payload("inbound_mms"), "Body": ""}

        (event,) = parse(connection, params)

        assert event.type == EventType.MESSAGE
        assert event.payload.media_ids

    def test_an_empty_callback_produces_nothing(self, connection: ChannelConnection) -> None:
        params = {**load_payload("inbound_text"), "Body": "", "NumMedia": "0"}

        assert parse(connection, params) == []

    def test_the_sender_is_the_identity_key(self, connection: ChannelConnection) -> None:
        """E.164 in, E.164 out — which is what lets an SMS identity link to
        ``contact.phone`` in ``apps.messaging.identities.ADDRESS_PLATFORMS``."""
        (event,) = parse(connection, load_payload("inbound_text"))

        assert event.platform_user_id.startswith("+")


class TestOptOutClassification:
    @pytest.mark.parametrize(
        "word",
        [
            "STOP",
            "stop",
            " Stop ",
            "STOP.",
            "STOP!",
            # ``strip(punctuation)`` halts on the space it exposes, so this one
            # used to come back as "stop " and match nothing.
            "STOP .",
            "STOP !",
            '"STOP"',
            "unsubscribe",
            "CANCEL",
            "End",
            "quit",
            "stopall",
        ],
    )
    def test_a_keyword_becomes_an_opt_out_event(self, connection: ChannelConnection, word: str) -> None:
        """The whole of the adapter's opt-out job (ROADMAP contract 3)."""
        events = parse(connection, {**load_payload("inbound_text"), "Body": word})

        assert [event.type for event in events] == [EventType.MESSAGE, EventType.OPT_OUT]

    def test_a_stop_is_a_thread_message_as_well_as_an_opt_out(self, connection: ChannelConnection) -> None:
        """``ingest`` writes no message row for an opt-out, so without the
        message half the conversation would show our confirmation and nothing
        the contact said."""
        message, opt_out = parse(connection, load_payload("inbound_stop"))

        assert message.type == EventType.MESSAGE
        assert message.payload.text.strip() == "Stop."
        assert opt_out.type == EventType.OPT_OUT
        # Distinct ids, or the event log's unique (connection, provider_event_id)
        # would drop the second as a duplicate delivery.
        assert message.provider_event_id != opt_out.provider_event_id
        assert opt_out.provider_event_id.endswith(message.provider_event_id)

    @pytest.mark.parametrize(
        "text",
        [
            "please stop sending these on Sundays",
            "stop by tomorrow",
            "I can't stop laughing",
            "endless",
            "cancellation policy?",
        ],
    )
    def test_a_sentence_containing_a_keyword_is_an_ordinary_message(
        self, connection: ChannelConnection, text: str
    ) -> None:
        """Substring matching here would be unrecoverable: only the contact can
        undo a suppression, and they have not been told how."""
        (event,) = parse(connection, {**load_payload("inbound_text"), "Body": text})

        assert event.type == EventType.MESSAGE

    def test_the_recorded_stop_fixture_is_an_opt_out(self, connection: ChannelConnection) -> None:
        assert EventType.OPT_OUT in [event.type for event in parse(connection, load_payload("inbound_stop"))]

    def test_help_is_an_ordinary_message_at_this_layer(self, connection: ChannelConnection) -> None:
        """HELP has no event type of its own — the hook reads the text. Making
        one up would mean a new member of a shared enum for one platform."""
        (event,) = parse(connection, load_payload("inbound_help"))

        assert event.type == EventType.MESSAGE
        assert keyword(event.payload.text) == "help"


class TestDeliveryStatus:
    def test_a_delivered_receipt(self, connection: ChannelConnection) -> None:
        (event,) = parse(connection, load_payload("status_delivered"))

        assert event.type == EventType.DELIVERY_STATUS
        assert event.payload.extra["provider_message_id"] == "SM66666666666666666666666666666666"
        assert event.payload.extra["status"] == "delivered"
        assert event.payload.extra["error"] == ""

    def test_undelivered_maps_to_failed_and_keeps_the_code(self, connection: ChannelConnection) -> None:
        (event,) = parse(connection, load_payload("status_undelivered"))

        assert event.payload.extra["status"] == "failed"
        assert event.payload.extra["error"] == "30006"

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [
            ("sent", "sent"),
            ("delivered", "delivered"),
            ("read", "read"),
            ("failed", "failed"),
            ("undelivered", "failed"),
        ],
    )
    def test_the_status_map(self, connection: ChannelConnection, reported: str, expected: str) -> None:
        from apps.channels.tests.sms_support import status_params

        (event,) = parse(connection, status_params(status=reported))

        assert event.payload.extra["status"] == expected

    def test_queued_emits_nothing(self, connection: ChannelConnection) -> None:
        """``apps.messaging.ingest.RECEIPT_STATUSES`` refuses it, so emitting
        one would cost a log row and a dedup insert to be discarded."""
        from apps.channels.tests.sms_support import status_params

        assert parse(connection, status_params(status="queued")) == []

    def test_each_status_dedups_separately(self, connection: ChannelConnection) -> None:
        """One message produces several callbacks. Keying on the SID alone would
        make all but the first look like a duplicate delivery."""
        from apps.channels.tests.sms_support import status_params

        (sent,) = parse(connection, status_params(status="sent"))
        (delivered,) = parse(connection, status_params(status="delivered"))

        assert sent.provider_event_id != delivered.provider_event_id

    def test_a_receipt_naming_no_message_is_dropped(self, connection: ChannelConnection) -> None:
        from apps.channels.tests.sms_support import status_params

        params = status_params()
        params.pop("MessageSid")
        params.pop("SmsSid")

        assert parse(connection, params) == []


class TestHostilePayloads:
    """SECURITY-BASELINE §2's mandatory half. Nothing here may raise."""

    @pytest.mark.parametrize(
        "name",
        [
            "hostile_injection",
            "hostile_types",
            "hostile_no_sender",
            "hostile_both_shapes",
            "inbound_text",
            "inbound_mms",
            "status_delivered",
        ],
    )
    def test_no_recorded_or_hostile_payload_raises(self, connection: ChannelConnection, name: str) -> None:
        assert isinstance(parse(connection, load_payload(name)), list)

    def test_injection_strings_are_carried_verbatim_and_never_executed(self, connection: ChannelConnection) -> None:
        """Stored as delivered; escaped at render. Nothing here interprets it."""
        (event,) = parse(connection, load_payload("hostile_injection"))

        assert "<script>" in event.payload.text
        assert "{{ 7*7 }}" in event.payload.text
        assert "49" not in event.payload.text

    def test_a_lying_num_media_is_clamped(self, connection: ChannelConnection) -> None:
        """9999 attachments claimed, two supplied — and ten is the ceiling."""
        (event,) = parse(connection, load_payload("hostile_types"))

        assert len(event.payload.media_ids) == 2
        assert len(event.payload.media_ids) <= MAX_MEDIA

    @pytest.mark.parametrize("value", ["", "  ", "abc", "-1", "1e400", "null"])
    def test_an_unparseable_num_media_yields_no_attachments(self, connection: ChannelConnection, value: str) -> None:
        params = {**load_payload("inbound_mms"), "NumMedia": value}

        (event,) = parse(connection, params)

        assert event.payload.media_ids == ()

    def test_a_callback_with_no_sender_is_dropped(self, connection: ChannelConnection) -> None:
        assert parse(connection, load_payload("hostile_no_sender")) == []

    def test_a_payload_claiming_to_be_both_is_decided_by_the_status(self, connection: ChannelConnection) -> None:
        """One rule, stated once — not "whichever key is read first"."""
        (event,) = parse(connection, load_payload("hostile_both_shapes"))

        assert event.type == EventType.DELIVERY_STATUS

    def test_an_unknown_status_produces_nothing(self, connection: ChannelConnection) -> None:
        params = {**load_payload("inbound_text"), "SmsStatus": "teleported"}
        params.pop("From")

        assert parse(connection, params) == []

    def test_a_status_less_payload_with_a_sender_is_still_a_message(self, connection: ChannelConnection) -> None:
        """A documented fallback, not a guess: the signature already proved this
        came from Twilio, so a missing status is a shape change on their side and
        dropping a real customer message over one is the worse failure."""
        params = {**load_payload("inbound_text")}
        params.pop("SmsStatus")

        (event,) = parse(connection, params)

        assert event.type == EventType.MESSAGE

    def test_an_over_long_body_is_bounded(self, connection: ChannelConnection) -> None:
        from apps.channels.providers.sms import MAX_INBOUND_TEXT_CHARS

        params = {**load_payload("inbound_text"), "Body": "x" * 100_000}

        (event,) = parse(connection, params)

        assert len(event.payload.text) == MAX_INBOUND_TEXT_CHARS

    def test_an_absurd_sender_is_hashed_not_truncated(self, connection: ChannelConnection) -> None:
        """Truncating would narrow an identity key without saying so, and two
        numbers agreeing on a long prefix would become one person."""
        params = {**load_payload("inbound_text"), "From": "+1" + "5" * 500}

        (event,) = parse(connection, params)

        assert event.platform_user_id.startswith("sha256:")
        assert len(event.platform_user_id) < 200

    def test_nul_bytes_do_not_reach_the_identity_key(self, connection: ChannelConnection) -> None:
        params = {**load_payload("inbound_text"), "From": "+1555\x00000"}

        (event,) = parse(connection, params)

        assert "\x00" not in event.platform_user_id

    def test_a_body_of_nothing_but_whitespace_is_not_a_keyword(self, connection: ChannelConnection) -> None:
        assert keyword("   ") == ""


class TestEndpoint:
    """The production path: a signed form POST reaching the real endpoint."""

    def test_a_signed_delivery_is_accepted_and_logged(self, client: Any, connection: ChannelConnection) -> None:
        from apps.channels.models import WebhookEventLog
        from apps.channels.tests.sms_support import signed_post

        response = signed_post(client, connection, load_payload("inbound_text"))

        assert response.status_code == 200
        assert WebhookEventLog.objects.filter(connection=connection).count() == 1

    def test_an_unsigned_delivery_is_403(self, client: Any, connection: ChannelConnection) -> None:
        response = client.post(webhook_path(connection), data=load_payload("inbound_text"))

        assert response.status_code == 403

    def test_a_redelivery_is_deduplicated(self, client: Any, connection: ChannelConnection) -> None:
        from apps.channels.models import WebhookEventLog
        from apps.channels.tests.sms_support import signed_post

        params = load_payload("inbound_text")
        signed_post(client, connection, params)
        signed_post(client, connection, params)

        assert WebhookEventLog.objects.filter(connection=connection).count() == 1

    def test_a_forged_status_callback_is_refused(self, client: Any, connection: ChannelConnection) -> None:
        """Anyone holding the webhook URL — it is in the Twilio console and on
        the settings page — must still not be able to mark a message delivered."""
        from apps.channels.tests.sms_support import signed_post, status_params

        response = signed_post(client, connection, status_params(), token="the-wrong-token")

        assert response.status_code == 403

    def test_the_form_body_is_not_treated_as_json(self, client: Any, connection: ChannelConnection) -> None:
        """``webhook_content = "form"`` is what keeps the endpoint from answering
        400 to every genuine Twilio delivery."""
        from apps.channels.tests.sms_support import signed_post

        assert TwilioAdapter.webhook_content == "form"
        assert signed_post(client, connection, load_payload("inbound_text")).status_code == 200

    def test_the_auth_token_never_reaches_a_log(self, client: Any, connection: ChannelConnection, caplog: Any) -> None:
        from apps.channels.tests.sms_support import signed_post

        with caplog.at_level("DEBUG"):
            signed_post(client, connection, load_payload("inbound_text"), token="the-wrong-token")

        assert AUTH_TOKEN not in caplog.text
