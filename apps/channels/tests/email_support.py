"""Test doubles for the three email backends.

The SMTP one is a **real server**, not a mock. The issue asks for "SMTP via a
local dummy", and the difference matters: Django's SMTP backend does a full
conversation — EHLO, STARTTLS negotiation, AUTH, MAIL FROM, RCPT TO, DATA — and
a mock that returns success proves only that we called a method. This one speaks
the protocol over a real socket on a loopback port, so the message that arrives
is the message the wire carried, headers and MIME structure included.

Roughly fifty lines rather than a dependency: ``aiosmtpd`` would be a new pin in
``requirements-dev.in`` for a server that only has to say "250 OK" a few times.
"""

import socketserver
import threading
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["DummySMTPServer", "FakeSESClient", "resend_transport"]


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    """One SMTP conversation, answered well enough for a real client."""

    def handle(self) -> None:
        server: Any = self.server
        server.connections += 1
        self._say("220 localhost BrightBean test SMTP")
        in_data = False
        awaiting_auth = False
        lines: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")

            if in_data:
                if line == ".":
                    server.messages.append("\r\n".join(lines))
                    lines = []
                    in_data = False
                    self._say("250 OK queued")
                    continue
                # Dot-stuffing: a body line starting with "." arrives doubled.
                lines.append(line[1:] if line.startswith("..") else line)
                continue

            if awaiting_auth:
                # The base64 blob of an AUTH LOGIN exchange. Its contents are of
                # no interest — what matters is that the client got this far.
                awaiting_auth = False
                self._say(server.auth_reply)
                continue

            command = line.split(" ", 1)[0].upper()
            if command == "EHLO":
                # AUTH is advertised because Django's backend calls login()
                # whenever a username and password are configured, and a server
                # that did not offer it would fail every credentialed connection
                # with SMTPNotSupportedError — which is a real failure mode, but
                # not the one these tests are about.
                self._say(f"250-localhost\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE {server.max_size}")
            elif command == "AUTH":
                # AUTH PLAIN carries the credential on the same line; AUTH LOGIN
                # asks for it over two more. Both end at `auth_reply`.
                if len(line.split(" ")) > 2:
                    self._say(server.auth_reply)
                else:
                    awaiting_auth = True
                    self._say("334 VXNlcm5hbWU6")
            elif command == "HELO":
                self._say("250 localhost")
            elif command == "MAIL":
                self._say(server.mail_from_reply)
            elif command == "RCPT":
                server.recipients.append(line)
                self._say(server.rcpt_reply)
            elif command == "DATA":
                self._say("354 End data with <CR><LF>.<CR><LF>")
                in_data = True
            elif command == "QUIT":
                self._say("221 Bye")
                return
            elif command == "RSET" or command == "NOOP":
                self._say("250 OK")
            else:
                self._say("502 Command not implemented")

    def _say(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8") + b"\r\n")
        self.wfile.flush()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass
class DummySMTPServer:
    """A loopback SMTP server, as a context manager.

    ``mail_from_reply`` and ``rcpt_reply`` are settable so a test can make the
    server refuse: a ``5xx`` is a permanent failure and a ``4xx`` a transient
    one, which is the split ``_deliver_smtp`` maps onto the send pipeline's
    retry policy.
    """

    mail_from_reply: str = "250 OK"
    rcpt_reply: str = "250 OK"
    auth_reply: str = "235 2.7.0 Authentication successful"
    max_size: int = 10_000_000
    messages: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    #: Conversations opened, so a test can prove the backend is pooled rather
    #: than reconnecting per message.
    connections: int = 0
    _server: Any = None
    _thread: Any = None

    def __enter__(self) -> "DummySMTPServer":
        self._server = _Server(("127.0.0.1", 0), _Handler)
        # Port 0 lets the OS pick, so parallel pytest workers cannot collide on
        # a fixed one — the same reason the test database is per-worker.
        self._server.messages = self.messages
        self._server.recipients = self.recipients
        self._server.mail_from_reply = self.mail_from_reply
        self._server.rcpt_reply = self.rcpt_reply
        self._server.auth_reply = self.auth_reply
        self._server.max_size = self.max_size
        self._server.connections = 0
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.connections = self._server.connections
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def credentials(self, **extra: Any) -> dict[str, Any]:
        """Connection credentials pointing at this server."""
        return {
            "provider": "smtp",
            "host": "127.0.0.1",
            "port": self.port,
            "security": "none",
            "username": "",
            "password": "",
            "from_address": "hello@sender.test",
            "from_name": "Sender",
            **extra,
        }


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


def resend_transport(
    *,
    status: int = 200,
    body: Any = None,
    headers: dict[str, str] | None = None,
    record: list[httpx.Request] | None = None,
) -> httpx.Client:
    """An ``httpx.Client`` answering every Resend call from a fixture.

    Returned rather than installed, because ``request_json`` takes a ``client``
    and closes only the ones it created — so a test hands one in and keeps it.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(
            status,
            json=body if body is not None else {"id": "resend-msg-1"},
            headers=headers or {},
        )

    return httpx.Client(transport=httpx.MockTransport(respond))


# ---------------------------------------------------------------------------
# SES
# ---------------------------------------------------------------------------


@dataclass
class FakeSESClient:
    """What ``boto3.client("sesv2")`` looks like from ``_deliver_ses``.

    ``error`` is a botocore-shaped response dict rather than an exception class,
    because that is what ``_ses_error`` reads — it deliberately never imports
    botocore's runtime-built exception types, and neither does this.
    """

    message_id: str = "ses-msg-1"
    error: dict[str, Any] | None = None
    sent: list[bytes] = field(default_factory=list)
    confirmed: list[dict[str, Any]] = field(default_factory=list)

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            raise _BotoError(self.error)
        self.sent.append(kwargs["Content"]["Raw"]["Data"])
        return {"MessageId": self.message_id}

    def get_account(self) -> dict[str, Any]:
        if self.error is not None:
            raise _BotoError(self.error)
        return {"SendingEnabled": True}

    def confirm_subscription(self, **kwargs: Any) -> dict[str, Any]:
        self.confirmed.append(kwargs)
        return {"SubscriptionArn": "arn:aws:sns:eu-west-1:1:topic:sub"}


class _BotoError(Exception):
    """A stand-in for a ``ClientError``: the ``response`` attribute is all we read."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__("boto failed")
        self.response = response
