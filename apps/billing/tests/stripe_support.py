"""A fake Stripe API, and the helpers every billing test needs.

Two things here, and neither should be rebuilt in a test module:

:func:`fake_stripe`
    Replaces ``apps.billing.stripe_client._client`` — the seam that module
    documents — with a real ``stripe.StripeClient`` whose *transport* is a
    recording fake. Nothing opens a socket, and everything above the socket is
    the real SDK: parameter construction, form encoding, response parsing and
    Stripe's own error mapping all run. That is the same argument
    ``apps/channels/tests/telegram_support.py`` makes for going through
    ``httpx.MockTransport`` rather than stubbing the call: a 429 really does
    become ``RateLimitError``, so a test about error handling tests error
    handling.

    It records the **form body Stripe actually received**, decoded back into a
    dict. That is what makes the most important assertion in this feature
    possible — that the price id which reached Stripe is the configured one and
    not something a form posted. A fake that recorded the Python arguments
    instead would pass while the encoding dropped a nested key.

:func:`signed_headers`
    Builds a genuine ``Stripe-Signature`` header. Webhook tests sign for real
    rather than asserting against a frozen digest, so the stale-timestamp test
    carries a *correct* signature and therefore proves the timestamp check
    rather than the signature check.
"""

import json
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl

#: Shaped like a real one, because the log-scrubbing patterns in
#: apps/common/logging.py are written for `sk_(live|test)_...` and a fixture
#: spelled "secret" would prove nothing about them.
SECRET_KEY = "sk_test_51Ab7CdEfGhIjKlMnOpQrStUvWxYz0123456789"  # noqa: S105 - a fake credential for tests
WEBHOOK_SECRET = "whsec_9f8e7d6c5b4a3210zyxwvutsrqponmlk"  # noqa: S105 - a fake credential for tests


@dataclass
class Reply:
    """What the fake answers for one request."""

    body: Any = field(default_factory=dict)
    status: int = 200

    def rendered(self) -> tuple[str, int, Mapping[str, str]]:
        return json.dumps(self.body), self.status, {"Content-Type": "application/json"}


@dataclass
class Recorded:
    """One request the SDK made, as Stripe would have received it."""

    method: str
    url: str
    headers: dict[str, str]
    form: dict[str, str]

    @property
    def path(self) -> str:
        """The path, without the scheme, host or query string."""
        without_scheme = self.url.split("://", 1)[-1]
        return "/" + without_scheme.split("/", 1)[1].split("?", 1)[0] if "/" in without_scheme else "/"


@dataclass
class FakeStripeAPI:
    """A recording fake. ``calls`` is every request, in order."""

    calls: list[Recorded] = field(default_factory=list)
    replies: dict[tuple[str, str], Reply] = field(default_factory=dict)
    default: Reply = field(default_factory=lambda: Reply(body={"id": "obj_default", "object": "object"}))

    def reply(self, method: str, path: str, body: Any, *, status: int = 200) -> None:
        """Answer ``METHOD /path`` with ``body``."""
        self.replies[(method.upper(), path)] = Reply(body=body, status=status)

    def calls_to(self, method: str, path: str) -> list[Recorded]:
        return [c for c in self.calls if c.method == method.upper() and c.path == path]

    def handle(self, method: str, url: str, headers: Mapping[str, str], post_data: Any) -> tuple[str, int, Any]:
        recorded = Recorded(
            method=method.upper(),
            url=url,
            headers=dict(headers),
            # post_data is exactly what would go on the wire: Stripe's
            # bracket-notation form encoding, e.g. "line_items[0][price]".
            form=dict(parse_qsl(post_data.decode() if isinstance(post_data, bytes) else (post_data or ""))),
        )
        self.calls.append(recorded)
        return self.replies.get((recorded.method, recorded.path), self.default).rendered()


def _transport(fake: FakeStripeAPI) -> Any:
    """A ``stripe`` HTTP client that answers from ``fake`` and never opens a socket."""
    from stripe._http_client import HTTPClient

    class RecordingHTTPClient(HTTPClient):  # type: ignore[misc]
        name = "fake"

        def request(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str] | None = None,
            post_data: Any = None,
            *,
            _usage: Any = None,
        ) -> tuple[str, int, Any]:
            return fake.handle(method, url, headers or {}, post_data)

        def request_with_retries(
            self,
            method: str,
            url: str,
            headers: Mapping[str, str],
            post_data: Any = None,
            max_network_retries: int | None = None,
            *,
            _usage: Any = None,
        ) -> tuple[str, int, Any]:
            return fake.handle(method, url, headers, post_data)

        def close(self) -> None:
            return None

    return RecordingHTTPClient(verify_ssl_certs=False)


@contextmanager
def fake_stripe(configure: Callable[[FakeStripeAPI], None] | None = None) -> Iterator[FakeStripeAPI]:
    """Run the block with every Stripe call answered in memory.

    Patches ``stripe_client._client`` — the documented seam — rather than the
    four call functions, so the real SDK runs underneath and the assertions in
    ``calls`` are about what Stripe would genuinely have received.
    """
    import stripe

    from apps.billing import stripe_client

    fake = FakeStripeAPI()
    if configure is not None:
        configure(fake)

    client = stripe.StripeClient(
        api_key=SECRET_KEY,
        stripe_version=stripe_client.STRIPE_API_VERSION,
        http_client=_transport(fake),
        max_network_retries=0,
    )
    original = stripe_client._client
    stripe_client._client = lambda: client  # type: ignore[assignment]
    try:
        yield fake
    finally:
        stripe_client._client = original  # type: ignore[assignment]
        stripe_client.reset_client()


def signed_headers(
    body: bytes,
    *,
    secret: str = WEBHOOK_SECRET,
    timestamp: int | None = None,
    extra_v1: tuple[str, ...] = (),
    scheme: str = "v1",
) -> dict[str, str]:
    """A genuine ``Stripe-Signature`` header for ``body``.

    ``extra_v1`` prepends further ``v1=`` values, which is what a secret rotation
    looks like on the wire: Stripe sends one signature per active secret, and a
    receiver that stops at the first one breaks every rotation.
    """
    import hashlib
    import hmac

    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    values = [f"{scheme}={extra}" for extra in extra_v1] + [f"{scheme}={digest}"]
    return {"HTTP_STRIPE_SIGNATURE": ",".join([f"t={ts}", *values])}
