"""A fake Instagram Graph API, and the fixtures that feed it.

The same three things every Instagram test needs and none of them should build
again, mirroring :mod:`apps.channels.tests.telegram_support`:

:func:`fake_graph`
    Routes the adapter's HTTP through ``httpx.MockTransport`` by replacing
    :func:`apps.channels.providers.instagram._client`, which is the seam that
    module documents for exactly this. Going through it rather than stubbing
    ``request_json`` means the **real** error mapping runs: a 429 really does
    become a ``RateLimitError`` carrying ``retry_after``, and a body with
    ``error.code`` really does reach ``APIError.code``.

:func:`load_delivery`
    A recorded webhook delivery from ``fixtures/instagram/``. Real payload
    shapes, so a passing test is a test against what Meta actually sends rather
    than against what we imagined it sends.

:func:`sign`
    The ``X-Hub-Signature-256`` a correctly configured Meta app would send.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from django.http import HttpRequest
from django.test import RequestFactory

from apps.channels import security
from apps.channels.providers import instagram, meta_common

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"

#: The Instagram professional account id every fixture is addressed to. It is
#: what the connection's ``external_id`` has to hold for ``resolve_connection``
#: to find the row.
IG_ACCOUNT_ID = "17841400000000001"

#: A commenter / sender id. Meta calls these IGSIDs and they look like this.
IG_USER_ID = "6789012345678901"

#: A token shaped the way Meta's are — ``IGAA`` and a long opaque tail — because
#: the log-scrubbing assertions are about a *shape*, and a token spelled
#: "secret" would prove nothing about them.
ACCESS_TOKEN = "IGAAQZBx1ExampleExampleExampleExampleExampleExampleExampleZD"  # noqa: S105 - a fake credential

#: The Meta app secret the fixtures are signed with.
#:
#: Deliberately **shapeless**, unlike :data:`ACCESS_TOKEN` above, and for the
#: reason ``conftest.secret_value`` gives: nothing about this value's *form*
#: is under test. The signature check is an HMAC over whatever it is given, and
#: the log scrubber recognises ``client_secret=`` by the key name rather than by
#: the value. A hex string here would look exactly like a real leaked key to
#: gitleaks and would have to be allowlisted for nothing in exchange.
APP_SECRET = "meta-app-secret-for-tests-only"  # noqa: S105 - a fake credential


def load_delivery(name: str) -> dict[str, Any]:
    """One recorded delivery, by fixture file name (without ``.json``)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def at_now(payload: dict[str, Any], *, offset_days: float = 0.0) -> dict[str, Any]:
    """The same delivery, restamped relative to now.

    The fixture files carry the timestamps they were recorded with, which is what
    makes them recordings. A test about SPEC §10's seven-day private-reply
    deadline has to control that clock, and a test that simply needs a *recent*
    comment would otherwise start failing a week after it was written — silently
    turning "the guard works" into "the fixture aged out".
    """
    from django.utils import timezone

    moment = timezone.now() - timedelta(days=offset_days)
    seconds = int(moment.timestamp())
    for entry in payload.get("entry", []):
        entry["time"] = seconds
        for item in entry.get("messaging", []):
            item["timestamp"] = seconds * 1000
    return payload


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    """The header value a correctly configured Meta app would send."""
    return f"sha256={security.sign_body(secret, body)}"


def request_for(payload: Any, *, secret: str | None = APP_SECRET) -> HttpRequest:
    """A request shaped the way the webhook endpoint hands one to an adapter."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = RequestFactory().post("/webhooks/instagram/", data=body, content_type="application/json")
    if secret is not None:
        # `headers` is a cached read-only view over META, so the signature goes
        # in the way a real request carries it and the view is rebuilt from it.
        header = "HTTP_" + meta_common.SIGNATURE_HEADER.upper().replace("-", "_")
        request.META[header] = sign(body, secret)
    return request


@dataclass
class Reply:
    """What the fake answers for one path."""

    body: dict[str, Any] | None = None
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx.Response:
        if self.body is not None:
            return httpx.Response(self.status, json=self.body, headers=self.headers)
        if self.status >= 400:
            return httpx.Response(
                self.status,
                json={"error": {"message": "Fake failure", "code": self.status, "type": "OAuthException"}},
                headers=self.headers,
            )
        return httpx.Response(self.status, json={"message_id": "mid.sent.1", "recipient_id": IG_USER_ID})


@dataclass
class FakeGraph:
    """A recording fake. ``calls`` is ``(path, body)`` in the order sent."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    replies: dict[str, Reply] = field(default_factory=dict)
    #: Answers any path with no reply of its own.
    default: Reply = field(default_factory=Reply)
    #: Authorization headers the fake saw, so a test can assert a token was used
    #: — and, more usefully, that it never reached a log.
    tokens: list[str] = field(default_factory=list)

    def reply(self, path: str, reply: Reply) -> None:
        self.replies[path] = reply

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == path]

    def paths(self) -> list[str]:
        return [name for name, _body in self.calls]

    def message_bodies(self) -> list[dict[str, Any]]:
        """Every real send through ``me/messages``, in order.

        Filtered on the presence of ``message``, which excludes the
        ``sender_action`` calls: SPEC §7.1 has the routing pipeline fire
        ``mark_seen`` and ``send_typing`` before an inline reply, and those go to
        the same endpoint. Counting them as messages is the mistake this helper
        exists to stop a test making.
        """
        return [body for body in self.bodies("me/messages") if "message" in body]

    def messages(self) -> list[dict[str, Any]]:
        """Every ``message`` object sent, in order. See :meth:`message_bodies`."""
        return [body["message"] for body in self.message_bodies()]

    def sender_actions(self) -> list[str]:
        """The courtesy calls, in order."""
        return [body["sender_action"] for body in self.bodies("me/messages") if "sender_action" in body]

    def handle(self, request: httpx.Request) -> httpx.Response:
        # The path is /<version>/<rest>; the adapter's own "path" is the rest.
        parts = request.url.path.lstrip("/").split("/", 1)
        path = parts[1] if len(parts) > 1 else parts[0]
        self.tokens.append(request.headers.get("Authorization", ""))
        body = json.loads(request.content) if request.content else {}
        self.calls.append((path, body))
        return self.replies.get(path, self.default).response()


@contextmanager
def fake_graph(configure: Callable[[FakeGraph], None] | None = None) -> Iterator[FakeGraph]:
    """Run the block with every Graph call answered by an in-memory fake."""
    fake = FakeGraph()
    if configure is not None:
        configure(fake)

    client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    original = instagram._client
    # One client for the whole block. `request_json` only closes a client it
    # created itself, so handing out a fresh one per call would leak them.
    instagram._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        instagram._client = original  # type: ignore[assignment]
        client.close()
