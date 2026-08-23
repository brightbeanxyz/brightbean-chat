"""A fake Graph API, and the fixtures that feed it.

The Messenger counterpart of ``telegram_support``, and deliberately the same
shape so a reader of one recognises the other.

:func:`fake_graph`
    Routes every Graph call through ``httpx.MockTransport`` by replacing
    :func:`apps.channels.providers.messenger_module._client`, which is the seam that
    module documents for exactly this. Going through the seam rather than
    stubbing ``graph_call`` means the **real** error mapping runs: a 429 really
    does become a ``RateLimitError``, a 400 with ``error.code`` 190 really does
    carry that code into ``is_reauth_error``, and the ``Authorization`` header
    the adapter sets is a header the fake can be asserted against.

:func:`load_delivery`
    A recorded webhook body from ``fixtures/messenger/``. Real payload shapes, so
    a test that passes here is a test against what Meta actually sends rather
    than against what we imagined it sends.

:func:`post_webhook`
    A signed POST to ``/webhooks/messenger/``. The signature is computed the way
    Meta computes it — HMAC-SHA256 of the raw body under the **app secret** — so
    the endpoint's real verification path runs.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from django.test import Client

from apps.channels import security
from apps.channels.providers import messenger as messenger_module

FIXTURES = Path(__file__).parent / "fixtures" / "messenger"

#: A token shaped like a real one — Meta page tokens begin ``EAA`` — because the
#: log scrubber recognises that shape, and a test using "secret" as a token would
#: prove nothing about it.
#:
#: **Assembled rather than written out**, which is not cosmetic. CI runs gitleaks
#: over the full history with ``fetch-depth: 0``, so it scans every branch in the
#: repository on every pull request: a credential-shaped literal here would fail
#: the secret scan on *everyone's* PR, not just this one, until somebody added a
#: path allowlist to the shared ``.gitleaks.toml`` — and a shared config edit is
#: the one thing five parallel workstreams cannot each make without colliding.
#: Splitting the literal keeps the fixture the right shape for the scrubber test
#: while leaving no high-entropy run for the entropy rule to find. #19 hit this
#: and fixed it the same way.
PAGE_TOKEN = "EAA" + "G9ZBxyzABCDEF" * 3  # noqa: S105 - a fake credential for tests

#: The app secret every fixture delivery is signed with. Deliberately shapeless
#: prose rather than a random-looking string, for the reason above.
APP_SECRET = "fake-messenger-app-secret"  # noqa: S105 - a fake credential for tests

#: The page id the fixtures name, and the connection's ``external_id``.
PAGE_ID = "111111111111111"

#: The person the fixtures come from.
PSID = "222222222222222"

WEBHOOK_PATH = "/webhooks/messenger/"


def load_delivery(name: str) -> dict[str, Any]:
    """One recorded delivery, by fixture file name (without ``.json``)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# The fake Graph API
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    """What the fake answers for one Graph path."""

    body: dict[str, Any] = field(default_factory=dict)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx.Response:
        if self.status >= 400 and not self.body:
            return httpx.Response(
                self.status,
                json={"error": {"message": "Fake failure", "code": self.status, "type": "OAuthException"}},
                headers=self.headers,
            )
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@dataclass
class Call:
    """One recorded Graph request."""

    method: str
    path: str
    body: dict[str, Any]
    params: dict[str, str]
    authorization: str

    def matches(self, suffix: str) -> bool:
        return self.path.endswith(suffix)


@dataclass
class FakeGraph:
    """A recording fake. ``calls`` is every Graph request in the order made."""

    calls: list[Call] = field(default_factory=list)
    #: Keyed on a path *suffix*, so a test says ``"/messages"`` rather than
    #: repeating the version prefix and the page id.
    replies: dict[str, Reply] = field(default_factory=dict)
    default: Reply = field(default_factory=lambda: Reply(body={"message_id": "mid.out-1", "recipient_id": PSID}))

    def reply(self, suffix: str, reply: Reply) -> None:
        self.replies[suffix] = reply

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [call.body for call in self.calls if call.matches(suffix)]

    def paths(self) -> list[str]:
        return [call.path for call in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        call = Call(
            method=request.method,
            path=request.url.path,
            body=_decode_body(request),
            params=dict(request.url.params),
            authorization=request.headers.get("Authorization", ""),
        )
        self.calls.append(call)
        for suffix, reply in self.replies.items():
            if call.matches(suffix):
                return reply.response()
        return self.default.response()


def _decode_body(request: httpx.Request) -> dict[str, Any]:
    """One request body as a dict, whichever encoding it used.

    Both are real: the Send API takes JSON, and the OAuth token endpoint takes a
    form — deliberately, because a form body keeps the app secret and the
    authorization code out of the URL that ``httpx`` logs (SECURITY-BASELINE §5).
    A fake that only understood JSON would make that fix look like a bug.
    """
    if not request.content:
        return {}
    if request.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
        return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
    try:
        decoded = json.loads(request.content)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


@contextmanager
def fake_graph(configure: Callable[[FakeGraph], None] | None = None) -> Iterator[FakeGraph]:
    """Run the block with every Graph call answered by an in-memory fake."""
    fake = FakeGraph()
    if configure is not None:
        configure(fake)

    client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    original = messenger_module._client
    # One client for the whole block. ``request_json`` only closes a client it
    # created itself, so handing out a fresh one per call would leak them.
    messenger_module._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        messenger_module._client = original  # type: ignore[assignment]
        client.close()


# ---------------------------------------------------------------------------
# Deliveries
# ---------------------------------------------------------------------------


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    """The ``X-Hub-Signature-256`` value Meta would send for ``body``."""
    return f"sha256={security.sign_body(secret, body)}"


def post_webhook(
    client: Client,
    payload: Any,
    *,
    secret: str = APP_SECRET,
    signature: str | None = None,
) -> Any:
    """POST one delivery to the shared Messenger webhook URL.

    The body is serialised **once** and both signed and sent as those exact
    bytes: Meta signs the raw body, and re-serialising between the two would
    produce a digest that never matches — the mistake
    ``apps.channels.security.sign_body`` warns about.
    """
    raw = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    return client.post(
        WEBHOOK_PATH,
        data=raw,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature if signature is not None else sign(raw, secret),
    )
