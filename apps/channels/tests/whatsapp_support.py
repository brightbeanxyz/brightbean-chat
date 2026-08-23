"""A fake Graph API, and the fixtures that feed it (issue #19).

The Telegram equivalent — ``telegram_support`` — established the shape and the
reasoning is identical:

:func:`fake_graph_api`
    Routes the adapter's HTTP through ``httpx.MockTransport``, so nothing opens
    a socket. It replaces :func:`apps.channels.providers.whatsapp._client`,
    which is the seam that module documents for exactly this — and going through
    it rather than stubbing ``request_json`` means the **real** error mapping
    runs: a 429 really does become a ``RateLimitError``, and Meta's nested
    ``error.code`` really is lifted onto the exception.

:func:`load_delivery`
    A recorded webhook envelope from ``fixtures/whatsapp/``. Real payload
    shapes, so a test that passes here is a test against what Meta actually
    sends rather than against what we imagined it sends.

:func:`post_delivery`
    A signed POST to ``/webhooks/whatsapp/``. The signature is computed the way
    Meta computes it — HMAC-SHA256 of the **raw body** under the app secret —
    so the endpoint's real verification runs rather than being bypassed.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from django.test import Client

from apps.channels import security
from apps.channels.models import ChannelConnection
from apps.channels.providers import whatsapp
from apps.common.platforms import Platform

FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp"

WEBHOOK_URL = "/webhooks/whatsapp/"

#: The ids the fixtures are written against.
PHONE_NUMBER_ID = "109876543210987"
WABA_ID = "102290129340398"
WA_ID = "447700900123"

#: What the adapter stores as ``platform_user_id`` for :data:`WA_ID`. The plus
#: is the whole reason a WhatsApp identity can link to a contact captured over
#: SMS — see ``providers.whatsapp._wa_identity``.
PLATFORM_USER_ID = f"+{WA_ID}"

#: A token shaped like a real Meta one, because the log scrubber recognises the
#: "EAA" prefix and a test using "secret" as a token would prove nothing about
#: it. **Patterned rather than random on purpose**: gitleaks' `generic-api-key`
#: rule scores entropy near a keyword like `token`, so a realistic-looking
#: random tail fails CI's secret scan on a value that is entirely invented. The
#: repeated body keeps the shape the scrubber matches while scoring low, which
#: is the fixture-side fix `.gitleaks.toml`'s header prefers to a fourth
#: allowlist entry — an allowlist would widen what the scanner ignores for the
#: whole file, permanently.
ACCESS_TOKEN = "EAA" + "deadbeef" * 4  # noqa: S105 - a fake credential for tests

#: The same shape Meta actually issues: base64url, so it carries ``-`` and
#: ``_``. Kept beside the plain one because the scrubber's pattern has to accept
#: both, and the all-alphanumeric fixture above cannot show that — an
#: alphanumeric-only rule matches it happily while failing to fire on a real
#: token at all. Patterned rather than random, for the reason above.
BASE64URL_ACCESS_TOKEN = "EAA" + "dead-beef_" * 3  # noqa: S105 - a fake credential for tests

#: The Meta app secret the fixtures are signed with. Supplied to the resolution
#: chain through ``PLATFORM_CREDENTIALS_FROM_ENV`` in :func:`app_secret_settings`.
APP_SECRET = "an-app-secret-for-tests"  # noqa: S105 - a fake credential for tests


def load_delivery(name: str) -> dict[str, Any]:
    """One recorded webhook envelope, by fixture file name (without ``.json``)."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def fixture_names() -> list[str]:
    """Every recorded envelope, for a parametrized sweep over all of them."""
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def make_connection(workspace: Any, *, phone_number_id: str = PHONE_NUMBER_ID) -> ChannelConnection:
    """A WhatsApp connection with credentials, ready to send and receive."""
    connection = ChannelConnection(
        workspace=workspace,
        platform=Platform.WHATSAPP.value,
        display_name="+1 555 000 1111",
        external_id=phone_number_id,
    )
    whatsapp.store_credentials(
        connection,
        token=ACCESS_TOKEN,
        waba_id=WABA_ID,
        phone_number_id=phone_number_id,
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


def app_secret_settings(secret: str = APP_SECRET) -> dict[str, Any]:
    """``override_settings`` kwargs putting a Meta app on the env level.

    The deployment level of the credential chain, which is where a self-hoster
    with one Meta app configures it. Going through the real chain rather than
    stubbing ``_app_secret`` is what keeps these tests honest about where the
    signing key comes from — including the part that surprises people: the chain
    only uses a level that is **complete**, and
    ``apps.credentials.models.REQUIRED_CREDENTIAL_KEYS`` requires an app id
    alongside the secret. A deployment that sets only the secret gets no
    credentials at all and every delivery fails verification.
    """
    return {"PLATFORM_CREDENTIALS_FROM_ENV": {Platform.WHATSAPP.value: {"app_id": "1234567890", "app_secret": secret}}}


def signature(body: bytes, secret: str = APP_SECRET) -> str:
    """The ``X-Hub-Signature-256`` value Meta would send for ``body``."""
    return f"sha256={security.sign_body(secret, body)}"


def post_delivery(
    client: Client,
    payload: Any,
    *,
    secret: str = APP_SECRET,
    sign: bool = True,
) -> Any:
    """POST a delivery to the shared WhatsApp webhook URL, signed like Meta's.

    ``payload`` may be a dict (encoded here) or raw bytes, which is how the
    hostile-payload tests send bodies no json encoder would produce.
    """
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {"X-Hub-Signature-256": signature(body, secret)} if sign else {}
    return client.post(WEBHOOK_URL, data=body, content_type="application/json", headers=headers)


@dataclass
class Reply:
    """What the fake answers for one Graph request."""

    body: dict[str, Any] = field(default_factory=dict)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def response(self) -> httpx.Response:
        if self.status >= 400 and not self.body:
            return httpx.Response(
                self.status,
                # Meta's error shape, nested — which is what
                # ``providers.base._error_code`` reads.
                json={"error": {"message": "Fake failure", "code": self.status, "type": "OAuthException"}},
                headers=self.headers,
            )
        return httpx.Response(self.status, json=self.body, headers=self.headers)


@dataclass
class FakeGraphAPI:
    """A recording fake. ``calls`` is ``(method, path, payload)`` in order."""

    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    #: Keyed by the last path segment, e.g. "messages" or "message_templates".
    replies: dict[str, Reply] = field(default_factory=dict)
    default: Reply = field(default_factory=lambda: Reply(body={"messages": [{"id": "wamid.SENT1"}]}))
    #: Authorization headers the fake was called with, so a test can assert the
    #: token was used — and, more usefully, that it never reached a log.
    authorizations: list[str] = field(default_factory=list)
    #: Query strings, for the GET-shaped calls (verification, status polling).
    queries: list[dict[str, str]] = field(default_factory=list)

    def reply(self, segment: str, reply: Reply) -> None:
        self.replies[segment] = reply

    def payloads(self, segment: str) -> list[dict[str, Any]]:
        return [payload for _method, path, payload in self.calls if path.rsplit("/", 1)[-1] == segment]

    def paths(self) -> list[str]:
        return [path for _method, path, _payload in self.calls]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.authorizations.append(request.headers.get("Authorization", ""))
        self.queries.append(dict(request.url.params))
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path, payload))
        return self.replies.get(path.rsplit("/", 1)[-1], self.default).response()


@contextmanager
def fake_graph_api(configure: Callable[[FakeGraphAPI], None] | None = None) -> Iterator[FakeGraphAPI]:
    """Run the block with every Graph call answered by an in-memory fake."""
    fake = FakeGraphAPI()
    if configure is not None:
        configure(fake)

    client = httpx.Client(transport=httpx.MockTransport(fake.handle))
    original = whatsapp._client
    # One client for the whole block. `request_json` only closes a client it
    # created itself, so handing out a fresh one per call would leak them.
    whatsapp._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        whatsapp._client = original  # type: ignore[assignment]
        client.close()
