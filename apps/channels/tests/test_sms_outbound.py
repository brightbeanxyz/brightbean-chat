"""Sending through Twilio: the wire payloads, the downgrade, and the error paths.

Two halves, and the split is the point of :func:`~apps.channels.providers.sms.wire_calls`
being pure. The first half is a **table**: an ``OutboundMessage`` in, form
bodies out, no HTTP and no database, so a reader can check a payload against
Twilio's documentation without reading the send loop. The second half runs the
real :meth:`~apps.channels.providers.sms.TwilioAdapter.send` over
``httpx.MockTransport`` through the module's own ``_client`` seam, so the real
error mapping, the real 429 handling and the real payload building all run
without a socket.
"""

from typing import Any

import pytest

from apps.channels import ingest as channels_ingest
from apps.channels.capabilities import capabilities_for
from apps.channels.events import (
    Button,
    MediaBlock,
    OutboundMessage,
    SendStatus,
    TextBlock,
)
from apps.channels.models import ChannelConnection
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.providers.sms import (
    MAX_TEXT_CHARS,
    TwilioAdapter,
    sender_params,
    webhook_url,
    wire_calls,
)
from apps.channels.tests.sms_support import (
    ACCOUNT_SID,
    AUTH_TOKEN,
    CONTACT_NUMBER,
    FROM_NUMBER,
    MESSAGING_SERVICE_SID,
    FakeTwilio,
    Reply,
    fake_twilio,
    sms_connection,
)
from apps.common.platforms import Platform
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

SENDER = {"From": FROM_NUMBER}


class _Identity:
    """The one attribute the adapter reads off L3-A's identity."""

    def __init__(self, address: str = CONTACT_NUMBER) -> None:
        self.platform_user_id = address


def text(body: str) -> OutboundMessage:
    return OutboundMessage(blocks=(TextBlock(text=body),))


class TestWirePayloads:
    """Pure: the form bodies, without HTTP."""

    def test_one_text_block(self) -> None:
        assert wire_calls(CONTACT_NUMBER, SENDER, text("Hi")) == [
            {"To": CONTACT_NUMBER, "From": FROM_NUMBER, "Body": "Hi"}
        ]

    def test_a_messaging_service_replaces_the_from_number(self, tenancy: Tenancy) -> None:
        connection = sms_connection(tenancy.workspace, messaging_service_sid=MESSAGING_SERVICE_SID)

        assert sender_params(connection) == {"MessagingServiceSid": MESSAGING_SERVICE_SID}
        assert "From" not in wire_calls(CONTACT_NUMBER, sender_params(connection), text("Hi"))[0]

    def test_text_blocks_are_joined_rather_than_sent_one_each(self) -> None:
        """Two SMS are two billed messages arriving out of order on a bad day,
        and a reader has no bubble to tell them apart."""
        message = OutboundMessage(blocks=(TextBlock(text="First"), TextBlock(text="Second")))

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, message)

        assert call["Body"] == "First\n\nSecond"

    def test_an_image_rides_with_the_text(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Look"), MediaBlock(kind="image", url="https://example.test/a.jpg"))
        )

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, message)

        assert call["Body"] == "Look"
        assert call["MediaUrl"] == ["https://example.test/a.jpg"]

    def test_media_alone_is_sendable(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://example.test/a.jpg"),))

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, message)

        assert "Body" not in call
        assert call["MediaUrl"] == ["https://example.test/a.jpg"]

    def test_a_non_image_media_block_is_ignored(self) -> None:
        """It cannot occur after the downgrade — which turns it into a caption
        plus a link — and sending it as MediaUrl would be rejected by Twilio."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Hi"), MediaBlock(kind="video", url="https://example.test/a.mp4"))
        )

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, message)

        assert "MediaUrl" not in call

    def test_over_long_text_splits_and_the_media_rides_on_the_first(self) -> None:
        """Repeating the picture on each part would send it twice and bill for both."""
        message = OutboundMessage(
            blocks=(
                TextBlock(text="word " * 500),
                MediaBlock(kind="image", url="https://example.test/a.jpg"),
            )
        )

        calls = wire_calls(CONTACT_NUMBER, SENDER, message)

        assert len(calls) > 1
        assert all(len(call["Body"]) <= MAX_TEXT_CHARS for call in calls)
        assert "MediaUrl" in calls[0]
        assert all("MediaUrl" not in call for call in calls[1:])

    def test_nothing_sendable_produces_no_call(self) -> None:
        assert wire_calls(CONTACT_NUMBER, SENDER, OutboundMessage()) == []
        assert wire_calls(CONTACT_NUMBER, SENDER, text("   ")) == []

    def test_no_recipient_or_no_sender_produces_no_call(self) -> None:
        assert wire_calls("", SENDER, text("Hi")) == []
        assert wire_calls(CONTACT_NUMBER, {}, text("Hi")) == []


class TestDowngrade:
    def test_buttons_become_numbered_options_in_the_body(self) -> None:
        """SMS declares ``buttons=False``, so the shared renderer numbers them
        and the contact types a digit back. Nothing here implements that."""
        message = OutboundMessage(
            blocks=(TextBlock(text="Pick one"),),
            buttons=(Button(id="a", label="Apples"), Button(id="b", label="Pears")),
        )

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, _downgraded(message))

        assert "Reply 1 for Apples" in call["Body"]
        assert "Reply 2 for Pears" in call["Body"]

    def test_a_url_button_is_inlined_as_a_link(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="Here"),),
            buttons=(Button(id="u", label="Docs", url="https://example.test/docs"),),
        )

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, _downgraded(message))

        assert "Docs: https://example.test/docs" in call["Body"]

    def test_an_image_survives_the_downgrade(self) -> None:
        """MMS is declared, so the renderer keeps the block rather than turning
        it into a link (``capabilities_for(Platform.SMS).image``)."""
        assert capabilities_for(Platform.SMS).image is True

        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://example.test/a.jpg"),))

        (call,) = wire_calls(CONTACT_NUMBER, SENDER, _downgraded(message))

        assert call["MediaUrl"] == ["https://example.test/a.jpg"]


def _downgraded(message: OutboundMessage) -> OutboundMessage:
    from apps.channels.downgrade import downgrade

    result = downgrade(message, capabilities_for(Platform.SMS))
    assert len(result.messages) == 1
    return result.messages[0]


class TestSend:
    def test_it_posts_to_the_messages_endpoint_with_a_status_callback(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        connection = sms_connection(tenancy.workspace)

        with fake_twilio() as fake:
            result = TwilioAdapter().send(connection, _Identity(), text("Hi"))

        (method, path, form) = fake.calls[0]
        assert method == "POST"
        assert path == f"/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"
        assert form["To"] == [CONTACT_NUMBER]
        assert form["Body"] == ["Hi"]
        assert form["StatusCallback"] == [webhook_url(connection)]
        assert result.status == SendStatus.SENT
        assert result.provider_message_id == "SM00000000000000000000000000000001"

    def test_it_authenticates_with_basic_auth(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        """Twilio's scheme, and the reason the token never appears in a URL."""
        connection = sms_connection(tenancy.workspace)

        with fake_twilio() as fake:
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        assert fake.authorizations[0].startswith("Basic ")

    def test_a_multi_part_message_sends_in_order_and_reports_the_last_id(
        self, tenancy: Tenancy, monkeypatch: Any
    ) -> None:
        connection = sms_connection(tenancy.workspace)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"sid": "SMlast"}))

        with fake_twilio(fake):
            result = TwilioAdapter().send(connection, _Identity(), text("word " * 500))

        assert len(fake.forms("Messages.json")) > 1
        assert result.provider_message_id == "SMlast"

    def test_no_recipient_fails_without_calling_anyone(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        connection = sms_connection(tenancy.workspace)

        with fake_twilio() as fake:
            result = TwilioAdapter().send(connection, _Identity(""), text("Hi"))

        assert result.status == SendStatus.FAILED
        assert result.error == "no_recipient"
        assert fake.calls == []

    def test_a_connection_with_no_sender_fails_here_rather_than_at_twilio(
        self, tenancy: Tenancy, monkeypatch: Any
    ) -> None:
        """Twilio's error text quotes the request; ours does not."""
        connection = sms_connection(tenancy.workspace, from_number="", suffix="-nosender")

        with fake_twilio() as fake:
            result = TwilioAdapter().send(connection, _Identity(), text("Hi"))

        assert result.error == "no_sender"
        assert fake.calls == []

    def test_an_empty_message_is_reported_rather_than_counted_as_sent(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        connection = sms_connection(tenancy.workspace)

        with fake_twilio() as fake:
            result = TwilioAdapter().send(connection, _Identity(), OutboundMessage())

        assert result.error == "empty_message"
        assert fake.calls == []

    def test_missing_credentials_raise_a_named_error(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        connection = sms_connection(tenancy.workspace, sid="", token="", suffix="-nocreds")

        with fake_twilio(), pytest.raises(APIError, match="no Twilio credentials"):
            TwilioAdapter().send(connection, _Identity(), text("Hi"))


class TestErrorMapping:
    """Inherited from ``providers/base``, exercised through the real path."""

    def test_a_429_becomes_a_rate_limit_error_carrying_retry_after(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        connection = sms_connection(tenancy.workspace)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 20429}, status=429, headers={"Retry-After": "7"}))

        with fake_twilio(fake), pytest.raises(RateLimitError) as caught:
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        assert caught.value.retry_after == 7.0

    def test_a_4xx_carries_twilios_own_code(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        """Twilio puts a bare ``code`` at the top level, which is why
        ``base._error_code`` reads one — so the inbox shows
        ``provider_rejected:21211`` rather than a bare 400."""
        connection = sms_connection(tenancy.workspace)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21211, "message": "Invalid 'To'"}, status=400))

        with fake_twilio(fake), pytest.raises(APIError) as caught:
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        assert caught.value.status_code == 400
        assert caught.value.code == "21211"

    def test_an_error_message_names_the_host_and_never_the_credentials(
        self, tenancy: Tenancy, monkeypatch: Any
    ) -> None:
        connection = sms_connection(tenancy.workspace)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21211, "message": f"token {AUTH_TOKEN}"}, status=400))

        with fake_twilio(fake), pytest.raises(APIError) as caught:
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        rendered = str(caught.value)
        assert "api.twilio.com" in rendered
        assert AUTH_TOKEN not in rendered
        assert ACCOUNT_SID not in rendered


class TestTwilioSideOptOut:
    def test_a_21610_records_an_opt_out_through_the_pipeline(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        """Twilio tracks opt-outs at its end too — a contact who replied STOP
        before this workspace existed. The adapter raises the event the pipeline
        applies rather than writing ``opted_out_at`` itself (contract 3)."""
        from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events
        from apps.messaging.models import ContactChannelIdentity

        connection = sms_connection(tenancy.workspace)
        channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21610, "message": "unsubscribed"}, status=400))

        with fake_twilio(fake), pytest.raises(APIError):
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get(
            channel_connection=connection, platform_user_id=CONTACT_NUMBER
        )
        assert identity.opted_out_at is not None
        assert identity.opt_in is False

    def test_a_provider_opt_out_is_never_confirmed(self, tenancy: Tenancy) -> None:
        """The loop this guard exists to break.

        Confirming a suppression Twilio already held means sending to somebody
        Twilio rejects with 21610 — which raises another opt-out event from
        inside the failing send, mints a fresh event id and idempotency key on
        the next second boundary, and (when the bucket defers the reply) gets
        picked up again by the worker. Nobody wrote to us, so there is nothing
        to confirm.
        """
        from apps.channels.providers.sms import KEYWORD_OPT_OUT, OPT_OUT_SOURCE_KEY

        connection = sms_connection(tenancy.workspace)
        seen: list[Any] = []
        channels_ingest.register_processor(lambda _c, events: seen.extend(events), name="capture")
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21610, "message": "unsubscribed"}, status=400))

        with fake_twilio(fake), pytest.raises(APIError):
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        (event,) = seen
        assert event.type == "opt_out"
        assert event.platform_user_id == CONTACT_NUMBER
        # No marker, so the hook suppresses without replying.
        assert event.payload.extra.get(OPT_OUT_SOURCE_KEY) != KEYWORD_OPT_OUT

    def test_any_other_error_records_nothing(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events
        from apps.messaging.models import ContactChannelIdentity

        connection = sms_connection(tenancy.workspace)
        channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
        fake = FakeTwilio()
        fake.reply("Messages.json", Reply({"code": 21211}, status=400))

        with fake_twilio(fake), pytest.raises(APIError):
            TwilioAdapter().send(connection, _Identity(), text("Hi"))

        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()

    def test_the_adapter_module_never_assigns_opted_out_at(self) -> None:
        """The AST scan in ``apps/messaging/tests/test_write_sites.py`` already
        fails the build over this. Stated here too, in the module that would be
        tempted, so the reason is next to the code rather than three apps away.
        """
        import ast
        import inspect

        from apps.channels.providers import sms as sms_module

        tree = ast.parse(inspect.getsource(sms_module))
        assigned = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "opted_out_at" not in assigned


class TestNoThrottle:
    def test_the_adapter_does_not_sleep(self) -> None:
        """The global limit is the connection's token bucket (``rate_default=1.0``)
        and the per-recipient one is SPEC §9.6's advisory lock. A timer here
        would be a sleep held *inside* that lock.

        Asserted over the AST rather than the source text, because the module
        docstring explains at length why there is no sleep and a substring check
        would trip over its own explanation.
        """
        import ast
        import inspect

        from apps.channels.providers import sms as sms_module

        tree = ast.parse(inspect.getsource(sms_module))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert "sleep" not in called

    def test_the_client_is_pooled_across_calls(self) -> None:
        """``request_json`` closes a client it created itself, so returning None
        would mean a fresh TCP connection and TLS handshake per call — paid on
        every message, on every part of a split one, and on the STOP/HELP
        confirmation that goes out inside the webhook request against SPEC
        §7.1's 1.5 s budget. ``telegram._client`` pools for the same reason."""
        from apps.channels.providers import sms as sms_module

        first = sms_module._client()
        second = sms_module._client()

        assert first is not None
        assert first is second

    def test_the_policy_row_carries_twilios_long_code_rate(self) -> None:
        from apps.channels.policy import policy_for

        assert policy_for(Platform.SMS).rate_default == 1.0
        assert policy_for(Platform.SMS).window_hours is None


class TestConnectionHelpers:
    def test_credentials_that_cannot_be_decrypted_read_as_absent(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        """A key rotation is a configuration problem, not a 500 on a webhook."""
        from apps.channels.providers.sms import account_sid

        connection = sms_connection(tenancy.workspace)
        monkeypatch.setattr(
            ChannelConnection,
            "credentials",
            property(lambda self: (_ for _ in ()).throw(ValueError("bad key"))),
        )

        assert account_sid(connection) == ""
