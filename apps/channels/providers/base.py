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

    from apps.channels.media import MediaSource
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
    """How long to wait, from the header or the body — whichever the platform used.

    Only the delta-seconds form of ``Retry-After`` is honoured. The HTTP-date
    form is legal and nobody's messaging API uses it; parsing dates from an
    attacker-adjacent header to derive a sleep duration is not worth the
    surface.

    The body is consulted when the header is absent because Telegram documents
    its answer as ``parameters.retry_after`` in the JSON and does not always
    send the header (SPEC §6.2: "on HTTP 429 honor ``retry_after``"). Without
    this the adapter would get ``retry_after=None`` and the send pipeline would
    fall back to generic backoff, which is exactly the guess the platform just
    told us not to make. Reading the body on a 429 costs nothing —
    :func:`_error_code` already does it one line later.

    A value is only believed if it is a non-negative, finite number. Anything
    else is the platform's problem and the caller's own backoff is better than a
    sleep derived from garbage.
    """
    header = _seconds(response.headers.get("Retry-After", "").strip())
    if header is not None:
        return header
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    parameters = body.get("parameters")
    source = parameters if isinstance(parameters, dict) else body
    return _seconds(source.get("retry_after"))


def _seconds(raw: Any) -> float | None:
    """``raw`` as a non-negative, finite number of seconds, or None."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # NaN fails every comparison, so `>= 0` already rejects it; infinity does
    # not, and would become a retry scheduled at the end of time.
    return value if 0 <= value < float("inf") else None


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

    def media_source(self, connection: "ChannelConnection", media_id: str) -> "MediaSource | None":
        """Where a ``media_id`` from :attr:`EventPayload.media_ids` can be fetched.

        Not in SPEC §6.1's list, and here for the same reason
        :meth:`on_disconnect` is: every platform has a version of it and the
        alternative is the same fetch written once per adapter. Two adapters
        store an identifier rather than a URL because the platform's address
        needs *this connection's* credentials — a Telegram ``file_id`` becomes a
        URL only after a ``getFile`` call, a Twilio ``MediaUrl`` answers 401
        without the Account SID — and only the adapter knows which of those it
        is.

        Return :class:`apps.channels.media.MediaSource`: a URL, plus any headers
        the fetch needs. **Not the bytes.** The download is identical on every
        platform and is done once, under the SSRF guard, by
        :func:`apps.channels.media.fetch_media`; an adapter that fetches media
        itself has stepped outside SECURITY-BASELINE §6's single call site.
        Credentials belong in ``headers``, never in the URL's userinfo, which
        the guard refuses.

        An adapter may make a platform API call here — that is what ``getFile``
        is — through :func:`request_json`, which is the right helper for it: a
        fixed host, built from constants and a stored token.

        Returning None means "nothing to fetch": no adapter support, no stored
        credentials, an id the platform no longer recognises. The caller turns
        it into a 404 and a tombstone, so an implementation may return None
        freely rather than raising, and should not raise for an ordinary
        platform refusal.

        The default returns None, so a platform that never fills ``media_ids``
        — which is most of them — writes nothing.
        """
        return None

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

    # -- lifecycle ----------------------------------------------------------

    def on_webhook_secret_rotated(self, connection: "ChannelConnection", secret: str) -> None:  # noqa: B027
        """Push a freshly minted webhook secret to the platform.

        For platforms that hold the secret themselves rather than having an
        operator paste it into a console — Telegram's ``setWebhook`` takes a
        ``secret_token`` over the API, so rotating without telling Telegram
        leaves a connection that can never be verified again and no screen an
        operator can fix it from.

        ``secret`` is the plaintext, and it is passed rather than read off the
        connection because this is the one moment it is readable. Implementations
        must not log it.

        Raising is meaningful here, unlike :meth:`on_disconnect`: the caller has
        already stored the new secret, so a failure means the connection is
        broken until it is rotated again, and it has to say so rather than
        report success.
        """

    def on_disconnect(self, connection: "ChannelConnection") -> None:  # noqa: B027
        """Tell the platform to stop sending, just before the row is deleted.

        Not part of SPEC §6.1's list, and here because every platform has some
        version of it — Telegram's ``deleteWebhook``, Meta's unsubscribe, a
        Twilio callback URL cleared — and the alternative is six special cases
        in ``apps.channels.views.connection_delete``. Empty by default, like
        the two indicators above: a platform with nothing to tell says nothing.

        Called on a **best-effort** basis. The connection is going away whether
        or not the platform agrees, so an implementation may raise and the
        caller will log and carry on; what it must not do is leave the operator
        unable to disconnect because a remote API is down.
        """
