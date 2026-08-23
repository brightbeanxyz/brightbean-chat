"""The three backends, each round-tripping a real send.

The acceptance criterion is "all three provider variants round-trip a send (SMTP
via local dummy, APIs mocked); plain-text alternative generated". SMTP goes over
a socket to a server that speaks the protocol (``email_support.DummySMTPServer``),
so what is asserted is the bytes that crossed the wire.

The other half of every test here is the compliance one: the ``List-Unsubscribe``
pair has to survive **each** transport. A provider that silently dropped it would
make every send non-compliant with nothing visible to say so, and the three
encode headers three different ways — Django's MIME builder, a JSON ``headers``
map, and raw MIME.
"""

import email
from typing import Any

import pytest

from apps.channels.providers import email_backends
from apps.channels.providers.email_backends import Envelope
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.channels.tests.email_support import DummySMTPServer, FakeSESClient, resend_transport

UNSUBSCRIBE = "https://app.test/u/tok/"


class Connection:
    """A ChannelConnection stand-in: credentials, a pk and a workspace id."""

    def __init__(self, credentials: dict[str, Any]) -> None:
        self.credentials = credentials
        self.pk = "conn-1"
        self.workspace_id = "ws-1"


def envelope(**overrides: Any) -> Envelope:
    fields: dict[str, Any] = {
        "to": "reader@example.test",
        "subject": "Subject line",
        "html": "<p>Hello <strong>there</strong></p>",
        "text": "Hello there",
        "from_address": "hello@sender.test",
        "from_name": "Sender",
        "headers": {
            "List-Unsubscribe": f"<{UNSUBSCRIBE}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "message_id": "<abc@sender.test>",
    }
    return Envelope(**{**fields, **overrides})


class TestProviderResolution:
    @pytest.mark.parametrize("value", ["smtp", "resend", "ses"])
    def test_a_known_provider_is_kept(self, value: str) -> None:
        assert email_backends.provider_for(Connection({"provider": value})) == value

    @pytest.mark.parametrize("value", ["", "SMTP ", None, 7, "carrier-pigeon", {"nested": 1}])
    def test_anything_else_falls_back_to_smtp(self, value: Any) -> None:
        """The value is interpolated into a URL, so it can only ever be one of three."""
        resolved = email_backends.provider_for(Connection({"provider": value}))
        assert resolved in email_backends.PROVIDERS

    def test_undecryptable_credentials_do_not_raise(self) -> None:
        class Broken:
            pk = "x"

            @property
            def credentials(self) -> dict[str, Any]:
                raise ValueError("nope")

        assert email_backends.credentials_of(Broken()) == {}


@pytest.fixture(autouse=True)
def _empty_smtp_pool() -> Any:
    """No pooled SMTP connection may cross a test boundary.

    The pool is thread-local and deliberately long-lived, so without this a
    backend opened against one test's dummy server would be found by the next.
    """
    email_backends._close_pooled()
    yield
    email_backends._close_pooled()


class TestSMTP:
    def test_a_send_round_trips_over_a_socket(self) -> None:
        with DummySMTPServer() as server:
            connection = Connection(server.credentials())
            returned = email_backends.deliver(connection, envelope())

        assert returned == "<abc@sender.test>"
        assert len(server.messages) == 1
        parsed = email.message_from_string(server.messages[0])
        assert parsed["Subject"] == "Subject line"
        assert parsed["From"] == "Sender <hello@sender.test>"
        assert parsed["To"] == "reader@example.test"

    def test_the_compliance_headers_reach_the_wire(self) -> None:
        with DummySMTPServer() as server:
            email_backends.deliver(Connection(server.credentials()), envelope())
        parsed = email.message_from_string(server.messages[0])
        assert parsed["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
        assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_both_alternatives_are_present_text_first(self) -> None:
        """RFC 2046 §5.1.4: a client shows the LAST part it can render."""
        with DummySMTPServer() as server:
            email_backends.deliver(Connection(server.credentials()), envelope())
        parsed = email.message_from_string(server.messages[0])
        assert parsed.is_multipart()
        assert _content_types(parsed) == ["text/plain", "text/html"]

    def test_a_5xx_reply_is_permanent(self) -> None:
        """SMTP numbers them the opposite way round from HTTP; 5xx means stop."""
        with DummySMTPServer(rcpt_reply="550 No such mailbox") as server, pytest.raises(APIError) as caught:
            email_backends.deliver(Connection(server.credentials()), envelope())
        assert caught.value.status_code == 400

    def test_a_4xx_reply_is_retryable(self) -> None:
        with DummySMTPServer(rcpt_reply="451 Try again later") as server, pytest.raises(APIError) as caught:
            email_backends.deliver(Connection(server.credentials()), envelope())
        assert caught.value.status_code == 503

    def test_an_unreachable_relay_is_retryable(self) -> None:
        """No status code at all, which the pipeline reads as transient."""
        connection = Connection(
            {"provider": "smtp", "host": "127.0.0.1", "port": 1, "security": "none", "from_address": "a@b.test"}
        )
        with pytest.raises(APIError) as caught:
            email_backends.deliver(connection, envelope())
        assert caught.value.status_code is None

    def test_no_host_is_refused_before_a_socket_is_opened(self) -> None:
        with pytest.raises(APIError, match="no SMTP host"):
            email_backends.deliver(Connection({"provider": "smtp"}), envelope())

    def test_the_connection_is_reused_across_sends(self) -> None:
        """One TCP connect, TLS handshake and AUTH for the whole batch.

        A fresh connection per message meant all three per email — ten of each
        per second at the platform's default rate, paid inside the worker slot
        the token bucket is holding.
        """
        with DummySMTPServer() as server:
            connection = Connection(server.credentials())
            for _ in range(3):
                email_backends.deliver(connection, envelope())
            email_backends._close_pooled()

        assert len(server.messages) == 3
        # Three messages, one conversation: the dummy records one RCPT line per
        # message but the greeting is what counts a connection, and a reused
        # backend issues exactly one.
        assert server.connections == 1

    def test_changed_settings_do_not_keep_the_old_socket(self) -> None:
        """The pool key is the settings, not just the connection id.

        An operator editing the host changes where this backend should be
        talking without changing which row it belongs to.
        """
        with DummySMTPServer() as first:
            connection = Connection(first.credentials())
            email_backends.deliver(connection, envelope())

        with DummySMTPServer() as second:
            # Same pk, different port — exactly what editing the connection does.
            connection.credentials = second.credentials()
            email_backends.deliver(connection, envelope())
            email_backends._close_pooled()

        assert len(first.messages) == 1
        assert len(second.messages) == 1

    def test_a_dropped_connection_is_retried_once_on_a_fresh_one(self) -> None:
        """An idle socket a relay has quietly closed looks like a dead one."""
        with DummySMTPServer() as server:
            connection = Connection(server.credentials())
            email_backends.deliver(connection, envelope())
            # Kill the pooled socket behind the backend's back, the way a relay
            # timing out an idle connection does.
            _, backend = email_backends._SMTP_POOL.entry
            backend.connection.close()

            email_backends.deliver(connection, envelope())
            email_backends._close_pooled()

        assert len(server.messages) == 2

    def test_verify_opens_and_closes_a_connection(self) -> None:
        with DummySMTPServer() as server:
            email_backends.verify_credentials(Connection(server.credentials()))

    def test_verify_reports_an_unreachable_server(self) -> None:
        connection = Connection({"provider": "smtp", "host": "127.0.0.1", "port": 1, "security": "none"})
        with pytest.raises(APIError):
            email_backends.verify_credentials(connection)

    @pytest.mark.parametrize(
        ("security", "use_tls", "use_ssl"),
        [("starttls", True, False), ("ssl", False, True), ("none", False, False)],
    )
    def test_the_encryption_modes_are_mutually_exclusive(self, security: str, use_tls: bool, use_ssl: bool) -> None:
        """Django's backend raises at use time if both flags are set."""
        settings = email_backends._smtp_settings(Connection({"security": security, "host": "h"}))
        assert settings["use_tls"] is use_tls
        assert settings["use_ssl"] is use_ssl

    def test_a_header_cannot_be_injected_through_the_subject(self) -> None:
        with DummySMTPServer() as server:
            email_backends.deliver(
                Connection(server.credentials()),
                envelope(subject="Hi\r\nBcc: victim@example.test"),
            )
        parsed = email.message_from_string(server.messages[0])
        assert parsed["Bcc"] is None
        assert "\r\n" not in str(parsed["Subject"])


class TestResend:
    def test_a_send_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []
        client = resend_transport(record=seen)
        monkeypatch.setattr(email_backends, "request_json", _through(client))
        connection = Connection({"provider": "resend", "api_key": "re_key", "from_address": "hello@sender.test"})

        assert email_backends.deliver(connection, envelope()) == "resend-msg-1"

        body = _json_body(seen[0])
        assert body["to"] == ["reader@example.test"]
        assert body["subject"] == "Subject line"
        assert body["html"] and body["text"]
        assert body["headers"]["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
        assert body["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert seen[0].headers["Authorization"] == "Bearer re_key"

    def test_a_429_becomes_a_rate_limit_with_the_providers_own_delay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = resend_transport(status=429, body={}, headers={"Retry-After": "37"})
        monkeypatch.setattr(email_backends, "request_json", _through(client))
        connection = Connection({"provider": "resend", "api_key": "re_key"})

        with pytest.raises(RateLimitError) as caught:
            email_backends.deliver(connection, envelope())
        assert caught.value.retry_after == 37

    def test_a_4xx_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = resend_transport(status=422, body={"message": "bad from"})
        monkeypatch.setattr(email_backends, "request_json", _through(client))
        with pytest.raises(APIError) as caught:
            email_backends.deliver(Connection({"provider": "resend", "api_key": "k"}), envelope())
        assert caught.value.status_code == 422

    def test_no_key_is_refused_before_a_call(self) -> None:
        with pytest.raises(APIError, match="no Resend API key"):
            email_backends.deliver(Connection({"provider": "resend"}), envelope())

    def test_verify_asks_for_the_domains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Any] = []
        client = resend_transport(body={"data": []}, record=seen)
        monkeypatch.setattr(email_backends, "request_json", _through(client))
        email_backends.verify_credentials(Connection({"provider": "resend", "api_key": "re_key"}))
        assert seen[0].url.path == "/domains"
        assert seen[0].method == "GET"


class TestSES:
    def _connection(self) -> Connection:
        return Connection(
            {
                "provider": "ses",
                "access_key_id": "AKIA0000000000000000",
                "secret_access_key": "s3cret",
                "region": "eu-west-1",
                "from_address": "hello@sender.test",
            }
        )

    def test_a_send_round_trips_as_raw_mime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeSESClient()
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: client)

        assert email_backends.deliver(self._connection(), envelope()) == "ses-msg-1"

        parsed = email.message_from_bytes(client.sent[0])
        assert parsed["Subject"] == "Subject line"
        # Raw, not Simple: this is the whole reason the backend builds the MIME
        # itself. SES's simple form would not carry these.
        assert parsed["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
        assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        assert _content_types(parsed) == ["text/plain", "text/html"]

    def test_throttling_becomes_a_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeSESClient(error={"Error": {"Code": "Throttling"}, "ResponseMetadata": {"HTTPStatusCode": 400}})
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: client)
        with pytest.raises(RateLimitError):
            email_backends.deliver(self._connection(), envelope())

    def test_a_4xx_is_permanent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeSESClient(
            error={"Error": {"Code": "MessageRejected"}, "ResponseMetadata": {"HTTPStatusCode": 400}}
        )
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: client)
        with pytest.raises(APIError) as caught:
            email_backends.deliver(self._connection(), envelope())
        assert caught.value.status_code == 400
        assert caught.value.code == "MessageRejected"

    def test_a_transport_failure_has_no_status_and_is_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("dns")

        monkeypatch.setattr(email_backends, "ses_client", boom)
        with pytest.raises(APIError) as caught:
            email_backends.deliver(self._connection(), envelope())
        assert caught.value.status_code is None

    @pytest.mark.parametrize(
        "credentials",
        [
            {"provider": "ses", "secret_access_key": "s", "region": "r"},
            {"provider": "ses", "access_key_id": "a", "region": "r"},
            {"provider": "ses", "access_key_id": "a", "secret_access_key": "s"},
        ],
        ids=["no key id", "no secret", "no region"],
    )
    def test_incomplete_credentials_are_refused_before_boto_is_imported(self, credentials: dict[str, Any]) -> None:
        with pytest.raises(APIError, match="complete set of SES credentials"):
            email_backends.ses_client(Connection(credentials))

    def test_verify_reads_the_account(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeSESClient()
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: client)
        email_backends.verify_credentials(self._connection())


class TestMessageIds:
    def test_it_uses_the_sending_domain(self) -> None:
        assert email_backends.new_message_id("sender.test").endswith("@sender.test>")

    def test_no_domain_still_produces_one(self) -> None:
        assert email_backends.new_message_id("").startswith("<")

    def test_a_hostile_domain_cannot_inject_a_header(self) -> None:
        assert "\n" not in email_backends.new_message_id("sender.test\r\nBcc: x@y.test")


def _through(client: Any) -> Any:
    """``request_json`` bound to a mock transport, keeping its error policy.

    Patching ``request_json`` itself rather than an httpx client because
    ``email_backends`` imports the function by name — and the point is to
    exercise the real error mapping, so the wrapper only supplies the transport.
    """
    from apps.channels.providers.base import request_json

    def call(method: str, url: str, **kwargs: Any) -> Any:
        kwargs.pop("client", None)
        return request_json(method, url, client=client, **kwargs)

    return call


def _json_body(request: Any) -> Any:
    import json

    return json.loads(request.content.decode("utf-8"))


def _content_types(message: Any) -> list[str]:
    """The parts' content types, in order. ``walk`` rather than ``get_payload``.

    ``get_payload`` is typed as returning a string or a list depending on
    whether the message is multipart, so iterating it directly does not
    type-check; ``walk`` always yields messages.
    """
    return [part.get_content_type() for part in message.walk() if not part.is_multipart()]
