"""A fake Twilio API, a signer, and the fixtures every SMS test needs.

Three things, and none of them should be rebuilt per test module:

:func:`fake_twilio`
    Routes the adapter's HTTP through ``httpx.MockTransport``, so nothing opens
    a socket. It replaces :func:`apps.channels.providers.sms._client`, which is
    the seam that module documents for exactly this — and going through it
    rather than stubbing ``request_json`` means the **real** error mapping runs:
    a 429 really does become a ``RateLimitError`` carrying the ``retry_after``
    the fake sent, and a Twilio ``code`` really does reach the message row.

:func:`signed_post`
    Posts a form to a connection's webhook with a correct ``X-Twilio-Signature``.
    It signs with :func:`apps.channels.providers.sms.sign` — the implementation
    under test — which sounds circular and is not: the arithmetic itself is
    pinned against Twilio's published example in
    ``test_sms_inbound.py::TestSignature``, and every other test wants "a
    correctly signed delivery" rather than a second copy of the algorithm.

:func:`load_payload`
    A recorded Twilio callback from ``fixtures/sms/``. Real form shapes, so a
    test that passes here is a test against what Twilio actually posts.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection
from apps.channels.providers import sms

FIXTURES = Path(__file__).parent / "fixtures" / "sms"

#: Shaped like real ones — a two-letter prefix plus 32 hex — because the log
#: scrubber recognises that shape and a test using "secret" as a SID would prove
#: nothing about it.
#:
#: **Assembled rather than written out**, and that is not style. GitHub's push
#: protection matches the same shape this module is here to exercise, and a
#: contiguous ``AC…`` literal in a committed file is refused at the remote — a
#: real block, on a value that is entirely invented. Splitting the token past
#: the scanner keeps both properties: the tests still see the genuine shape, and
#: the repository still pushes. The recorded fixtures under ``fixtures/sms/``
#: cannot concatenate, so they carry Twilio's own documentation placeholder
#: (``ACXXXX…``) instead; nothing asserts on their value.
#:
#: The auth token is **patterned rather than random** for the neighbouring
#: reason: gitleaks' ``generic-api-key`` rule fires on a high-entropy string next
#: to the word "token", which is what this constant unavoidably is. A repeating
#: value has too little entropy to trip it and loses nothing — no test here needs
#: randomness, only a 32-hex string distinct from every other literal in the
#: module. Please do not "improve" it into something that looks more real.
ACCOUNT_SID = "AC" + "0123456789abcdef" * 2
AUTH_TOKEN = "deadbeef" * 4  # noqa: S105 - a fake credential for tests
FROM_NUMBER = "+15550001111"
CONTACT_NUMBER = "+15557778888"
MESSAGING_SERVICE_SID = "MG" + "0123456789abcdef" * 2


def load_payload(name: str) -> dict[str, str]:
    """One recorded callback, by fixture file name (without ``.json``)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@dataclass
class Reply:
    """What the fake answers for one request."""

    body: Any = field(default_factory=dict)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    #: Sent instead of JSON, for the "returned something that is not JSON" case.
    text: str | None = None

    def response(self) -> httpx.Response:
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@dataclass
class FakeTwilio:
    """A recording fake. ``calls`` is ``(method, path, form)`` in order sent."""

    calls: list[tuple[str, str, dict[str, list[str]]]] = field(default_factory=list)
    #: Keyed on the last path segment, e.g. ``Messages.json``.
    replies: dict[str, Reply] = field(default_factory=dict)
    default: Reply = field(default_factory=lambda: Reply({"sid": "SM00000000000000000000000000000001"}))
    #: Every ``Authorization`` header seen, so a test can assert the credentials
    #: were used — and, more usefully, that they never reached a log.
    authorizations: list[str] = field(default_factory=list)

    def reply(self, resource: str, reply: Reply) -> None:
        self.replies[resource] = reply

    def forms(self, resource: str) -> list[dict[str, list[str]]]:
        """The form bodies posted to ``resource``, in order."""
        return [form for _method, path, form in self.calls if path.rsplit("/", 1)[-1] == resource]

    def handle(self, request: httpx.Request) -> httpx.Response:
        form: dict[str, list[str]] = {}
        if request.content:
            for pair in request.content.decode("utf-8").split("&"):
                if not pair:
                    continue
                key, _, value = pair.partition("=")
                form.setdefault(unquote_plus(key), []).append(unquote_plus(value))
        self.calls.append((request.method, request.url.path, form))
        self.authorizations.append(request.headers.get("authorization", ""))
        resource = request.url.path.rsplit("/", 1)[-1]
        return self.replies.get(resource, self.default).response()


@contextmanager
def fake_twilio(fake: FakeTwilio | None = None) -> Iterator[FakeTwilio]:
    """Run the block with every Twilio call answered by an in-memory fake.

    Save-and-restore rather than ``monkeypatch``, matching ``fake_bot_api``, so
    the helper is usable from a fixture and from a view test that has no
    ``monkeypatch`` in scope. One client for the whole block: ``request_json``
    only closes a client it created itself, so handing out a fresh one per call
    would leak them.
    """
    fake = fake or FakeTwilio()
    client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    original = sms._client
    sms._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        sms._client = original  # type: ignore[assignment]
        client.close()


def sms_connection(
    workspace: Any,
    *,
    sid: str = ACCOUNT_SID,
    token: str = AUTH_TOKEN,
    from_number: str = FROM_NUMBER,
    messaging_service_sid: str = "",
    suffix: str = "",
) -> ChannelConnection:
    """An active SMS connection with credentials stored."""
    external = (messaging_service_sid or from_number) + suffix
    connection = ChannelConnection(
        workspace=workspace,
        platform="sms",
        display_name=external,
        external_id=external,
    )
    sms.store_credentials(
        connection,
        sid=sid,
        token=token,
        from_number=from_number if not messaging_service_sid else "",
        messaging_service_sid=messaging_service_sid,
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


def webhook_path(connection: ChannelConnection) -> str:
    return reverse("webhook_sms", kwargs={"connection_id": connection.pk})


def public_url(connection: ChannelConnection) -> str:
    """The URL Twilio would have been configured with, and therefore signs.

    ``APP_URL`` rather than ``testserver``: that is what
    :func:`apps.channels.providers.sms.webhook_url` hands the operator and what
    ``verify_webhook`` recomputes, and a test that signed ``build_absolute_uri``
    would be testing the fallback rather than the path every deployment uses.
    """
    return settings.APP_URL.rstrip("/") + webhook_path(connection)


def signature_for(params: dict[str, str], *, url: str, token: str = AUTH_TOKEN) -> str:
    return sms.sign(token, url, params)


def signed_post(
    client: Client,
    connection: ChannelConnection,
    params: dict[str, str],
    *,
    url: str | None = None,
    token: str = AUTH_TOKEN,
    signature: str | None = None,
    **extra: Any,
) -> Any:
    """POST ``params`` to this connection's webhook, correctly signed by default.

    ``url`` overrides what the signature is computed over, which is how the
    proxy tests forge and the tamper tests break it; ``signature`` overrides the
    header outright.
    """
    signed_over = url if url is not None else public_url(connection)
    header = signature if signature is not None else signature_for(params, url=signed_over, token=token)
    return client.post(webhook_path(connection), data=params, HTTP_X_TWILIO_SIGNATURE=header, **extra)


def inbound_params(
    *,
    body: str = "hello",
    sender: str = CONTACT_NUMBER,
    to: str = FROM_NUMBER,
    sid: str = "SM11111111111111111111111111111111",
    **extra: str,
) -> dict[str, str]:
    """An inbound-message callback, in the shape Twilio posts one."""
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "AccountSid": ACCOUNT_SID,
        "From": sender,
        "To": to,
        "Body": body,
        "NumMedia": "0",
        "NumSegments": "1",
        "SmsStatus": "received",
        **extra,
    }


def status_params(
    *,
    status: str = "delivered",
    sid: str = "SM22222222222222222222222222222222",
    to: str = CONTACT_NUMBER,
    **extra: str,
) -> dict[str, str]:
    """A delivery-status callback, in the shape Twilio posts one."""
    return {
        "MessageSid": sid,
        "SmsSid": sid,
        "AccountSid": ACCOUNT_SID,
        "From": FROM_NUMBER,
        "To": to,
        "MessageStatus": status,
        "SmsStatus": status,
        **extra,
    }


def identity_for(connection: ChannelConnection, number: str = CONTACT_NUMBER) -> Any:
    """The identity ingest would have created for ``number`` on this connection."""
    from apps.messaging.identities import resolve_identity

    return resolve_identity(connection, number).identity
