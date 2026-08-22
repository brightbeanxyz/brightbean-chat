"""The adapter interface every platform implements (SPEC §6.1).

    class Adapter:
        capabilities: Capabilities   # static per platform
        def verify_webhook(self, request, connection) -> bool
        def parse_events(self, request, connection) -> list[NormalizedEvent]
        def send(self, connection, identity, outbound: OutboundMessage) -> SendResult
        def send_typing(self, connection, identity) -> None      # no-op where unsupported
        def mark_seen(self, connection, identity) -> None        # no-op where unsupported

That is the whole contract, and it is reproduced above so a Layer-5 author can
check their class against the specification without leaving the file. Modelled
on BrightBean Studio's ``providers/base.py``: an abstract base with the
platform-specific work abstract and the shared HTTP mechanics concrete.

**One addition SPEC §7.1 forces.** Meta-style platforms share a single webhook
URL per deployment and "connection resolved from payload ids", so there is a
step before ``verify_webhook`` that the §6.1 list does not name:
:meth:`resolve_connection`. It defaults to None, which the endpoint treats
exactly like a failed signature check — see ``apps.channels.views_webhooks``.

**No adapter and no outbound call ship in this layer.** The HTTP helper below is
here because contract 4 promises a Layer-5 platform costs "one module and one
registry line", and that is only true if the timeout policy, the 429 handling
and the error mapping already exist and are the same for everyone. It is
exercised by ``tests/test_providers_base.py`` through ``httpx.MockTransport``;
nothing in the webhook path calls it.
"""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Literal

import httpx

from apps.channels.capabilities import Capabilities
from apps.channels.events import NormalizedEvent, OutboundMessage, SendResult
from apps.channels.providers.exceptions import APIError, RateLimitError

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.channels.models import ChannelConnection

logger = logging.getLogger(__name__)

__all__ = ["Adapter", "request_json"]

#: Connect and read timeouts, in seconds. SPEC §7.1 budgets 1.5 s of wall clock
#: for an inline send with "2 s hard timeout on the HTTP client"; that is the
#: read timeout. Connect is shorter because a platform that has not completed a
#: TCP handshake in two seconds is not about to answer in time either.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 2.0

#: The timeout for work that is *not* on the inline path — an OAuth exchange, a
#: template submission, a media upload. Studio uses 30 s throughout.
BACKGROUND_TIMEOUT = 30.0


def request_json(
    method: str,
    url: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    client: httpx.Client | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """One HTTP call to a platform API, with this project's error policy.

    Returns the decoded JSON body. Raises :class:`RateLimitError` on 429 —
    carrying ``retry_after`` from the response header where the platform sent
    one — and :class:`APIError` on any other non-2xx, on a transport failure and
    on a body that is not JSON.

    ``client`` is the seam the tests use: pass an ``httpx.Client`` built on a
    ``MockTransport`` and no socket is opened. Adapters call it without one.

    The URL is **not** user-supplied — it is built by the adapter from constants
    and stored ids — so this is not an SSRF call site and does not need the
    guard from issue #15 (SECURITY-BASELINE §6). An adapter that ever wants to
    fetch a contact- or user-supplied URL must use that guard instead, and must
    not reach for this function.
    """
    effective_timeout = timeout if timeout is not None else httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
    owned = client is None
    http = client or httpx.Client(timeout=effective_timeout)
    try:
        response = http.request(method, url, timeout=effective_timeout, **kwargs)
    except httpx.TimeoutException as exc:
        raise APIError(f"{method} {_host(url)} timed out") from exc
    except httpx.HTTPError as exc:
        # type(exc).__name__ rather than str(exc): httpx puts the full URL —
        # query string and any token in it — into transport error messages.
        raise APIError(f"{method} {_host(url)} failed: {type(exc).__name__}") from exc
    finally:
        if owned:
            http.close()

    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitError(
            f"{_host(url)} is rate limiting this connection",
            retry_after=_retry_after(response),
            code=_error_code(response),
        )
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise APIError(
            f"{method} {_host(url)} was rejected",
            status_code=response.status_code,
            code=_error_code(response),
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise APIError(f"{_host(url)} returned a non-JSON body", status_code=response.status_code) from exc
    if not isinstance(payload, dict):
        raise APIError(f"{_host(url)} returned {type(payload).__name__}, expected an object")
    return payload


def _host(url: str) -> str:
    """The host of ``url`` and nothing else.

    Error messages name the host rather than the URL because the path and query
    of a platform call routinely carry an access token, and these messages are
    logged and shown in the inbox (SECURITY-BASELINE §5).
    """
    try:
        return httpx.URL(url).host or "the platform"
    except (httpx.InvalidURL, ValueError, TypeError):
        return "the platform"


def _retry_after(response: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, if the platform sent a usable one.

    Only the delta-seconds form is honoured. The HTTP-date form is legal and
    nobody's messaging API uses it; parsing dates from an attacker-adjacent
    header to derive a sleep duration is not worth the surface.
    """
    raw = response.headers.get("Retry-After", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _error_code(response: httpx.Response) -> str:
    """The platform's machine-readable error code, best effort.

    Meta nests it at ``error.code``; Telegram uses ``error_code``. Anything
    unparseable yields an empty string — this is decoration on an error path and
    must never raise on top of the failure it is describing.
    """
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict) and error.get("code") is not None:
        return str(error["code"])[:64]
    if body.get("error_code") is not None:
        return str(body["error_code"])[:64]
    return ""


class Adapter(ABC):
    """One platform's implementation of SPEC §6.1.

    Subclasses are instantiated per use and hold no per-connection state: every
    method takes the connection it operates on. That is what lets
    :func:`apps.channels.registry.adapter_for` hand out a fresh instance without
    caring whether the caller is a webhook request or a worker.
    """

    #: The platform this adapter serves. Must be a value of
    #: ``apps.common.platforms.Platform``.
    platform: str = ""

    #: The static capability record, from ``apps.channels.capabilities``.
    #: Declared here so ``adapter.capabilities`` reads as SPEC §6.1 writes it,
    #: while the table itself stays importable without this module.
    capabilities: Capabilities

    #: How this platform encodes its webhook body. Meta and Telegram post JSON;
    #: Twilio posts a form. The endpoint uses this to decide whether a body that
    #: will not parse as JSON is a malformed payload (400) or perfectly normal,
    #: so an adapter that gets it wrong makes every delivery fail visibly rather
    #: than silently parsing nothing.
    webhook_content: Literal["json", "form"] = "json"

    # -- inbound ------------------------------------------------------------

    def resolve_connection(self, request: "HttpRequest", raw_body: bytes) -> "ChannelConnection | None":
        """Which connection this delivery belongs to, before verifying it.

        Only the shared-URL platforms need this: SPEC §7.1 gives Meta one
        ``/webhooks/<platform>/`` per deployment and resolves the connection
        from ids inside the payload, and Telegram identifies itself by the
        secret in ``X-Telegram-Bot-Api-Secret-Token``. The per-connection routes
        (``/webhooks/sms/<connection_id>/``) never call this.

        Returning None is not an error the caller distinguishes from a bad
        signature — both answer the same 403 — so an implementation may return
        None freely rather than raising.
        """
        return None

    @abstractmethod
    def verify_webhook(self, request: "HttpRequest", connection: "ChannelConnection") -> bool:
        """True when this request really came from the platform.

        Implementations compare over the **raw body**, before any JSON parsing,
        with a constant-time comparison — ``apps.channels.security`` has the
        helpers. Returning False makes the endpoint answer 403 and count a
        signature failure against the caller's throttle.
        """

    @abstractmethod
    def parse_events(self, request: "HttpRequest", connection: "ChannelConnection") -> list[NormalizedEvent]:
        """Turn a verified payload into normalized events.

        Defensive by requirement (SECURITY-BASELINE §2): every field is
        attacker-controlled, so type-check what you read, tolerate missing and
        extra keys, and drop an event you cannot understand rather than raising
        — one malformed event in a batch must not cost the whole delivery.
        """

    # -- outbound -----------------------------------------------------------

    @abstractmethod
    def send(self, connection: "ChannelConnection", identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message. ``identity`` is L3-A's ContactChannelIdentity.

        Implementations run ``outbound`` through
        :func:`apps.channels.downgrade.downgrade` first; the renderer is shared
        precisely so six adapters do not each invent their own approximation.
        """

    # B027 flags an empty non-abstract method on an ABC, on the theory that it
    # is an abstract method someone forgot to mark. Here the empty body IS the
    # specification: SPEC §6.1 lists both as "no-op where unsupported", and
    # making them abstract would force four adapters to write `pass` to say
    # nothing.
    def send_typing(self, connection: "ChannelConnection", identity: Any) -> None:  # noqa: B027
        """Show a typing indicator. No-op where the platform has none."""

    def mark_seen(self, connection: "ChannelConnection", identity: Any) -> None:  # noqa: B027
        """Mark the conversation read. No-op where the platform has none."""
