"""Twilio SMS/MMS adapter (SPEC §6.6) — inbound and outbound, BYO account.

Written against the template :mod:`apps.channels.providers.telegram` set out to
be, and to the same division: everything that is *about being an adapter* is
inherited and everything that is *about Twilio* lives in a small named helper.

Inherited, and deliberately not re-implemented here:

* the HTTP mechanics, the timeout policy, the ``429`` → :class:`RateLimitError`
  mapping and the "never put a URL in an error message" rule, from
  :mod:`apps.channels.providers.base`;
* block downgrading, from :func:`apps.channels.downgrade.downgrade` — SMS
  declares ``buttons=False``, so the shared renderer turns a button row into the
  numbered options SPEC §6.1 specifies and hands back the answer key;
* the compliance decision, which is :mod:`apps.channels.policy`'s row for SMS
  read as data by :func:`apps.messaging.compliance.can_send`. There is no SMS
  branch anywhere in ``apps/messaging/``.

--------------------------------------------------------------------------
Carrier compliance, and what belongs in this file
--------------------------------------------------------------------------

SPEC §6.6 requires STOP/HELP/START to be handled **in core, before trigger
matching**, so no flow can bypass them (SPEC §19). That is split across two
modules on purpose, and the split is not stylistic:

*This* module classifies a STOP keyword as :attr:`EventType.OPT_OUT` and stops
there. ROADMAP contract 3 gives ``identity.opted_out_at`` exactly one write site
— ``apps/messaging/ingest.py`` — and ``apps/messaging/tests/test_write_sites.py``
fails the build over a second one, so an adapter that "handled" an opt-out by
writing the column would be a red build rather than a design choice. Ingest
already applies the column from an ``OPT_OUT`` event.

:mod:`apps.channels.sms_compliance` owns the other half: the ``hard_optout``
hook that sends the confirmation, answers HELP, and re-subscribes on START. The
keyword vocabularies live *here* because they are facts about SMS carriers, and
:func:`parse_events` needs :data:`OPT_OUT_KEYWORDS` itself.

--------------------------------------------------------------------------
Rate limits, and why there is no throttle in this file
--------------------------------------------------------------------------

A Twilio long code sends about one message per second. That number is already in
``apps.channels.policy`` as ``rate_default=1.0`` and is enforced by the
connection's token bucket (``apps.messaging.buckets``); the per-recipient limit
is satisfied by SPEC §9.6 serialising everything a contact does behind one
advisory lock. A timer here would be a sleep held *inside* that lock. When
Twilio disagrees anyway it answers ``429`` and the send pipeline reschedules.

--------------------------------------------------------------------------
Secrets
--------------------------------------------------------------------------

The auth token is the account: anyone holding it can send as the number and read
every message. It lives encrypted in ``connection.credentials`` and appears at
runtime in exactly two places — an ``Authorization: Basic`` header, which never
reaches a log, and the HMAC key in :func:`verify_webhook`, which never leaves
this module. Unlike Telegram's, it never goes in a URL. The account SID *is* in
the URL path, so ``apps.common.logging`` scrubs its shape
(SECURITY-BASELINE §5), and ``base.request_json`` reports the host of a failed
call and never the path.
"""

import base64
import hashlib
import hmac
import logging
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels import security
from apps.channels.capabilities import Capabilities, capabilities_for
from apps.channels.downgrade import downgrade, split_text
from apps.channels.events import (
    EventPayload,
    EventType,
    MediaBlock,
    NormalizedEvent,
    OutboundMessage,
    SendResult,
    SendStatus,
    TextBlock,
)
from apps.channels.models import ChannelConnection
from apps.channels.providers.base import BACKGROUND_TIMEOUT, Adapter, request_json
from apps.channels.providers.exceptions import APIError
from apps.channels.registry import register_adapter
from apps.common.net import is_trusted_proxy
from apps.common.platforms import Platform

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "ACCOUNT_SID_KEY",
    "AUTH_TOKEN_KEY",
    "FROM_NUMBER_KEY",
    "HELP_KEYWORDS",
    "MESSAGING_SERVICE_KEY",
    "OPT_IN_KEYWORDS",
    "OPT_OUT_KEYWORDS",
    "SIGNATURE_HEADER",
    "TwilioAdapter",
    "account_sid",
    "auth_token",
    "fetch_account",
    "fetch_messaging_service",
    "fetch_number",
    "sender_params",
    "sign",
    "store_credentials",
    "webhook_url",
    "wire_calls",
]

#: The REST API root. A constant rather than a setting, for the same reason
#: Telegram's is: a configurable host on a path carrying the account SID, called
#: with the account's credentials attached, is an exfiltration primitive.
API_ROOT = "https://api.twilio.com"

#: The Messaging API root, which is a different host from the one above and
#: hosts messaging services (``MG…``).
MESSAGING_ROOT = "https://messaging.twilio.com"

#: The API version prefix Twilio has used since 2010 and has never changed.
API_VERSION = "2010-04-01"

#: The header Twilio signs every webhook with.
SIGNATURE_HEADER = "X-Twilio-Signature"  # noqa: S105 - a header name, not a credential

#: Where the credentials sit inside ``connection.credentials``.
ACCOUNT_SID_KEY = "account_sid"
AUTH_TOKEN_KEY = "auth_token"  # noqa: S105 - a dict key, not a credential
FROM_NUMBER_KEY = "from_number"
MESSAGING_SERVICE_KEY = "messaging_service_sid"

#: The capability row, read from the shared table rather than restated so the
#: two cannot drift. SMS carries text and MMS images and nothing else.
_CAPABILITIES: Capabilities = capabilities_for(Platform.SMS)
MAX_TEXT_CHARS = _CAPABILITIES.max_text_len

#: Longest inbound body we carry out of a parse. ``apps.messaging.ingest`` bounds
#: it again downstream; this one exists so a hostile payload cannot make us hold
#: an arbitrarily long string in the first place (SECURITY-BASELINE §§2, 7).
MAX_INBOUND_TEXT_CHARS = MAX_TEXT_CHARS

#: Media per MMS. Twilio's own ceiling is 10; a payload claiming more is a
#: payload we stop reading rather than one we believe.
MAX_MEDIA = 10

#: Longest media URL kept. Matches ``apps.messaging.ingest``'s own cap, so a URL
#: this adapter carries is one that layer will still store.
MAX_MEDIA_URL_CHARS = 2000

#: Longest attacker-supplied display string kept in ``payload.extra``.
MAX_EXTRA_CHARS = 200

#: The width of the ``platform_user_id`` column. ``apps.messaging.identities``
#: hashes rather than truncates past it, and so does everything upstream.
MAX_PLATFORM_ID_CHARS = 200

#: SPEC §6.6's hard opt-out keywords. Matched case-insensitively on the trimmed
#: body and nothing else: "stop" is an unsubscribe, "stop by tomorrow" is a
#: sentence, and treating the second as the first is how a real conversation
#: ends in a suppression list. ``STOPALL`` is Twilio's own synonym and is here
#: because a contact who types it has unmistakably asked to be left alone.
OPT_OUT_KEYWORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})

#: SPEC §6.6's HELP keyword. Answered even from an opted-out identity, which is
#: the whole reason ``apps.messaging.services.send_compliance_reply`` exists.
HELP_KEYWORDS = frozenset({"help"})

#: SPEC §6.6's re-subscribe keywords. The **only** door back in: an operator
#: cannot un-say an opt-out (``services.record_opt_out`` says so), so consent
#: has to come from the contact the way it did the first time.
OPT_IN_KEYWORDS = frozenset({"start", "unstop"})

#: What Twilio calls a delivery state, mapped onto SPEC §5's ``message.status``.
#:
#: ``queued`` is absent deliberately. ``apps.messaging.ingest.RECEIPT_STATUSES``
#: refuses it — a message is put into ``queued`` by *us*, before anyone is
#: called, and letting a receipt write it would walk a failed message backwards
#: and clear its error — so emitting one here would only be discarded a layer
#: later, having cost a log row and a dedup insert on the way.
DELIVERY_STATUSES: dict[str, str] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "failed",
    "failed": "failed",
}

#: The status Twilio stamps on an inbound message.
INBOUND_STATUS = "received"

#: Twilio's code for "this recipient has opted out", which its own platform
#: tracks alongside ours. Treated the way Telegram treats a 403: it means never
#: send here again, and continuing to try is what gets a number blocked.
UNSUBSCRIBED_RECIPIENT_CODE = "21610"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _credentials(connection: ChannelConnection) -> dict[str, Any]:
    """The connection's decrypted credentials, or ``{}``.

    ``credentials`` is an encrypted column, so reading it can fail on a
    deployment whose key has changed. That is a configuration problem and not
    something a webhook or a send should turn into a 500, so it reads as "no
    credentials" and the caller fails the operation with a named error.
    """
    try:
        stored: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return {}
    return stored if isinstance(stored, dict) else {}


def _credential(connection: ChannelConnection, key: str) -> str:
    value = _credentials(connection).get(key)
    return value if isinstance(value, str) else ""


def account_sid(connection: ChannelConnection) -> str:
    """The Twilio account SID (``AC…``) this connection sends through."""
    return _credential(connection, ACCOUNT_SID_KEY)


def auth_token(connection: ChannelConnection) -> str:
    """The auth token. Never logged, never rendered, never put in a URL."""
    return _credential(connection, AUTH_TOKEN_KEY)


def sender_params(connection: ChannelConnection) -> dict[str, str]:
    """``From`` or ``MessagingServiceSid`` — whichever this connection holds.

    Twilio accepts either and not both. A messaging service is the right answer
    for anyone sending at volume (it owns the number pool, the A2P campaign and
    sticky sender), a bare number for everyone else, and which one a workspace
    configured is a fact about their Twilio account rather than a preference
    this product should have.
    """
    service = _credential(connection, MESSAGING_SERVICE_KEY)
    if service:
        return {"MessagingServiceSid": service}
    number = _credential(connection, FROM_NUMBER_KEY)
    return {"From": number} if number else {}


def store_credentials(
    connection: ChannelConnection,
    *,
    sid: str,
    token: str,
    from_number: str = "",
    messaging_service_sid: str = "",
) -> None:
    """Put the Twilio credentials on the encrypted column.

    The only place they are written, mirroring ``telegram.store_bot_token``.
    Both exist so the encrypted-JSON column is reached through named functions:
    ``EncryptedJSONField`` subclasses ``TextField``, so django-stubs types the
    attribute as ``str`` and every direct assignment is a type error even though
    the column holds JSON. One suppression here beats one per call site.
    """
    connection.credentials = {  # type: ignore[assignment]
        ACCOUNT_SID_KEY: sid,
        AUTH_TOKEN_KEY: token,
        FROM_NUMBER_KEY: from_number,
        MESSAGING_SERVICE_KEY: messaging_service_sid,
    }


# ---------------------------------------------------------------------------
# The REST client
# ---------------------------------------------------------------------------


#: The process-wide connection pool. Built lazily and never closed: it lives as
#: long as the process does, which is the point.
_POOL: httpx.Client | None = None

_POOL_LOCK = threading.Lock()


def _client() -> httpx.Client | None:
    """The HTTP client every Twilio call goes through.

    A **pooled** client, for the reason ``telegram._client`` spells out:
    ``request_json``'s default of one client per call means a fresh TCP
    connection and a fresh TLS handshake for every call — a hundred or more
    milliseconds — and on this adapter that cost lands in three places that can
    least afford it. Every outbound message pays it once; a message split across
    the 1600-character cap pays it per part; and the STOP/HELP confirmation pays
    it **inside the webhook request**, against SPEC §7.1's 1.5 s inline budget,
    on the one interaction a carrier requires to be prompt.

    Built lazily rather than at import, so a forked worker gets its own pool
    rather than inheriting sockets opened before the fork. ``httpx.Client`` is
    safe to share across threads; the lock only keeps two threads from building
    two pools on the first call. Per-call timeouts still win — ``request_json``
    passes ``timeout=`` to ``request()``, which overrides the client's own — so
    the 30-second connect-time calls and the 2-second send are both unaffected.

    **This is also the test seam**, mirroring ``request_json``'s ``client=``
    parameter: a test replaces this function with one returning an
    ``httpx.Client`` on a ``MockTransport``, and the whole module — real error
    mapping, real 429 handling, real payload building — runs without a socket.
    """
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = httpx.Client(limits=httpx.Limits(max_keepalive_connections=4, max_connections=16))
    return _POOL


def call(
    connection: ChannelConnection,
    method: str,
    url: str,
    *,
    data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """One Twilio REST call, authenticated as this connection's account.

    HTTP Basic with ``(account_sid, auth_token)`` — Twilio's own scheme. The
    token is in a header, never in the URL, which is why nothing in this module
    needs Telegram's care about logging paths.

    Raises :class:`APIError` (or :class:`RateLimitError` on a 429) through
    ``request_json``, which also lifts Twilio's top-level ``code`` onto the
    exception so a failure reaches the inbox as ``provider_rejected:21610``
    rather than as a bare 400.
    """
    sid = account_sid(connection)
    token = auth_token(connection)
    if not sid or not token:
        raise APIError("This SMS connection has no Twilio credentials stored.")
    return request_json(
        method,
        url,
        data=data,
        params=params,
        auth=(sid, token),
        client=_client(),
        timeout=timeout,
    )


def _account_url(sid: str, resource: str) -> str:
    return f"{API_ROOT}/{API_VERSION}/Accounts/{sid}/{resource}"


def fetch_account(sid: str, token: str) -> dict[str, Any]:
    """Prove a SID and token work, and say which account they belong to.

    Called by the connect flow before anything is written, so a wrong credential
    leaves no trace. Takes the pair directly rather than a connection, because
    at that point there is no connection yet.
    """
    return request_json(
        "GET",
        f"{API_ROOT}/{API_VERSION}/Accounts/{sid}.json",
        auth=(sid, token),
        client=_client(),
        timeout=BACKGROUND_TIMEOUT,
    )


def fetch_number(sid: str, token: str, number: str) -> dict[str, Any]:
    """Confirm ``number`` is a number this account owns.

    Twilio answers a *list*, empty when the account does not hold it, so this
    returns the first entry or raises — an operator who typed a number they do
    not own has a connection that would fail on its first send, and finding that
    out at connect time is the whole point of validating.
    """
    body = request_json(
        "GET",
        _account_url(sid, "IncomingPhoneNumbers.json"),
        params={"PhoneNumber": number, "PageSize": 1},
        auth=(sid, token),
        client=_client(),
        timeout=BACKGROUND_TIMEOUT,
    )
    numbers = body.get("incoming_phone_numbers")
    if not isinstance(numbers, list) or not numbers or not isinstance(numbers[0], dict):
        raise APIError("That Twilio account does not hold that number.")
    return numbers[0]


def fetch_messaging_service(sid: str, token: str, service_sid: str) -> dict[str, Any]:
    """Confirm a messaging service (``MG…``) exists on this account."""
    return request_json(
        "GET",
        f"{MESSAGING_ROOT}/v1/Services/{service_sid}",
        auth=(sid, token),
        client=_client(),
        timeout=BACKGROUND_TIMEOUT,
    )


def webhook_url(connection: ChannelConnection) -> str:
    """The absolute URL Twilio delivers this connection's traffic to.

    ``settings.APP_URL`` — the deployment's own configured address — rather than
    anything derived from a request, following ``telegram.webhook_url`` and
    ``apps.media_library.delivery``. Three callers need to agree on this string
    or the product breaks quietly: the settings page that tells an operator what
    to paste, the ``StatusCallback`` on every outbound message, and
    :func:`verify_webhook`, which recomputes an HMAC **over the URL itself**. A
    page that showed one host while the adapter verified another would reject
    every genuine delivery with nothing to say why.
    """
    path = reverse("webhook_sms", kwargs={"connection_id": connection.pk})
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def sign(token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: base64 HMAC-SHA1 over the URL plus params.

    The scheme, exactly: take the full URL Twilio was configured to call, append
    every POST parameter as ``key`` immediately followed by ``value`` in
    **key-sorted** order, and HMAC-SHA1 the result under the account's auth
    token.

    SHA-1 is Twilio's choice, not ours, and it is used here as an HMAC — where
    the collision weaknesses that retired SHA-1 for signatures do not apply.
    There is no alternative on offer: the signature has to match what Twilio
    computed.
    """
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()  # noqa: S324
    return base64.b64encode(digest).decode("ascii")


def public_urls(request: "HttpRequest") -> tuple[str, ...]:
    """The URLs this request may have been signed against, most likely first.

    Twilio signs **the URL an operator pasted into its console**, which behind a
    reverse proxy is not ``request.build_absolute_uri()``: the proxy terminates
    TLS and rewrites the host, so the request the application sees says
    ``http://127.0.0.1:8000`` while Twilio signed ``https://chat.example.com``.
    Verifying against the wrong one rejects every genuine delivery.

    So the candidates, in order:

    1. ``settings.APP_URL`` plus this request's own path. The deployment's
       configured address, and the exact string :func:`webhook_url` told the
       operator to paste — so on any correctly configured deployment this is the
       one that matches and the list ends here.
    2. A proxy-declared origin, and **only** when the peer is a configured
       trusted proxy (``apps.common.net.is_trusted_proxy``). ``X-Forwarded-*``
       is attacker-controlled otherwise: any client can claim any host, and a
       forged host would let a caller pick the URL their forged signature was
       computed over. That check is what keeps this from being a bypass.
    3. ``request.build_absolute_uri()``, for a deployment with no proxy in front
       of it and an ``APP_URL`` that does not match what Twilio was given.

    Trying more than one costs one extra HMAC over a few hundred bytes and
    weakens nothing: each candidate is a full constant-time comparison against a
    key the caller does not have.
    """
    path = request.get_full_path()
    candidates = [_join(settings.APP_URL, path)]

    forwarded = _forwarded_origin(request)
    if forwarded:
        candidates.append(_join(forwarded, path))

    candidates.append(request.build_absolute_uri())

    seen: dict[str, None] = {}
    for candidate in candidates:
        if candidate:
            seen.setdefault(candidate, None)
    return tuple(seen)


def _join(origin: str, path: str) -> str:
    """``origin`` + ``path``, tolerating either having or lacking a slash."""
    if not origin:
        return ""
    return urljoin(origin.rstrip("/") + "/", path.lstrip("/"))


def _forwarded_origin(request: "HttpRequest") -> str:
    """``scheme://host`` from ``X-Forwarded-*``, or "" when it may not be trusted.

    Fails closed at every step: an untrusted peer, a missing host, or a host
    carrying anything but the characters a hostname and port are made of yields
    "" and the candidate is simply not offered.
    """
    peer = (request.META.get("REMOTE_ADDR") or "").strip()
    if not is_trusted_proxy(peer):
        return ""

    host = (request.META.get("HTTP_X_FORWARDED_HOST") or request.META.get("HTTP_HOST") or "").strip()
    # The leftmost entry is the original client's, which for a host header is
    # the one the caller asked for.
    host = host.split(",")[0].strip()
    # ``isascii()`` as well as ``isalnum()``: the latter alone is true for every
    # Unicode letter and digit, so "évil.example" and a fullwidth-"e" homograph
    # both passed a check that reads as an ASCII allowlist and was not one.
    if not host or not all((char.isascii() and char.isalnum()) or char in ".-:[]" for char in host):
        return ""

    scheme = (request.META.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip().lower()
    if scheme not in ("http", "https"):
        scheme = "https" if request.is_secure() else "http"
    return f"{scheme}://{host}"


# ---------------------------------------------------------------------------
# Outbound: OutboundMessage -> Messages API calls
# ---------------------------------------------------------------------------


def wire_calls(to: str, sender: dict[str, str], message: OutboundMessage) -> list[dict[str, Any]]:
    """The ``Messages.json`` form bodies for one already-downgraded message.

    Pure: no HTTP, no database, no clock. That is what lets the send-payload
    tests be a table and what lets a reader check a body against Twilio's
    documentation without reading the send loop.

    ``message`` is expected to have been through
    :func:`apps.channels.downgrade.downgrade` already, so buttons are already
    numbered options inside the text and cards and galleries are gone. Media
    blocks that are not images cannot occur for the same reason — the renderer
    turns them into a caption plus a link — and are ignored rather than sent as
    an ``MediaUrl`` Twilio would reject.

    Text blocks are joined rather than sent one call each: unlike a chat app,
    two SMS are two billed messages arriving out of order on a bad day, and a
    reader has no bubble to tell them apart. Over the 1600-character ceiling the
    shared :func:`~apps.channels.downgrade.split_text` splits them, which is the
    same word-boundary rule everything else in the project uses.
    """
    if not to or not sender:
        return []

    parts = [block.text.strip() for block in message.blocks if isinstance(block, TextBlock) and block.text.strip()]
    media = [
        block.url for block in message.blocks if isinstance(block, MediaBlock) and block.kind == "image" and block.url
    ][:MAX_MEDIA]

    body = "\n\n".join(parts)
    bodies = split_text(body, MAX_TEXT_CHARS) if body else []
    if not bodies and not media:
        return []

    calls: list[dict[str, Any]] = []
    for index, text in enumerate(bodies or [""]):
        payload: dict[str, Any] = {"To": to, **sender}
        if text:
            payload["Body"] = text
        # The media rides on the first call only. Repeating it on a split
        # message would send the same picture two or three times and bill for
        # each of them.
        if not index and media:
            payload["MediaUrl"] = media
        calls.append(payload)
    return calls


# ---------------------------------------------------------------------------
# Inbound: form post -> NormalizedEvent
# ---------------------------------------------------------------------------


def _text(value: Any, limit: int = MAX_INBOUND_TEXT_CHARS) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return value[:limit] if isinstance(value, str) else ""


def _address(value: Any) -> str:
    """A bounded ``platform_user_id``, or "".

    An absurdly long one is **hashed, not truncated**, which is the rule this
    codebase applies to every identifier — see
    ``apps.messaging.identities.bounded_key``. Truncating narrows an identity
    key without saying so, and two numbers agreeing on their first 200
    characters would become one person receiving another's conversation. Not
    reachable from a real Twilio payload; the point is that it cannot become
    reachable.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\x00", "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= MAX_PLATFORM_ID_CHARS:
        return cleaned
    return f"sha256:{hashlib.sha256(cleaned.encode('utf-8')).hexdigest()}"


def keyword(body: str) -> str:
    """The compliance keyword in ``body``, or "".

    Trimmed and case-folded, and matched against the **whole** body. SPEC §6.6's
    keywords are the message, not a substring of it: a contact writing "please
    stop sending these on Sundays" has not sent a STOP command, and suppressing
    them on a substring match would be an unrecoverable mistake — only they can
    undo it, and they have not been told how.

    Surrounding punctuation is stripped because "STOP." and "STOP!" are the same
    request and a carrier would treat them as such.

    Whitespace is stripped **again afterwards**, and that is not belt-and-braces:
    ``strip(chars)`` stops at the first character outside its set, so "STOP ."
    lost the dot and then halted on the space it exposed, leaving "stop " — a
    string that matches no keyword and quietly left the contact subscribed.
    """
    return body.strip().strip(".!?,;:'\"").strip().casefold()


def _media_urls(params: dict[str, str]) -> tuple[str, ...]:
    """``MediaUrl0…`` for an MMS, bounded and never fetched.

    ``NumMedia`` says how many there are and is attacker-supplied like the rest,
    so it bounds the loop only after being clamped: a payload claiming a
    thousand attachments gets ten reads, not a thousand.

    The URLs are **recorded, never fetched**. SECURITY-BASELINE §6 forbids a
    server-side fetch of a platform-supplied URL outside the SSRF guard, and
    these particular ones need the account's own credentials to retrieve.
    """
    try:
        count = int(params.get("NumMedia") or 0)
    except (TypeError, ValueError):
        return ()
    urls = []
    for index in range(max(0, min(count, MAX_MEDIA))):
        url = _text(params.get(f"MediaUrl{index}"), MAX_MEDIA_URL_CHARS).strip()
        if url:
            urls.append(url)
    return tuple(urls)


def _extra(params: dict[str, str]) -> dict[str, Any]:
    """Display detail worth keeping. Attacker-controlled: escape on render."""
    extra: dict[str, Any] = {}
    for key, source in (("city", "FromCity"), ("state", "FromState"), ("country", "FromCountry")):
        value = _text(params.get(source), MAX_EXTRA_CHARS).strip()
        if value:
            extra[key] = value
    return extra


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class TwilioAdapter(Adapter):
    """SPEC §6.6, implemented against SPEC §6.1's interface."""

    platform = Platform.SMS.value
    capabilities = _CAPABILITIES
    #: Twilio posts ``application/x-www-form-urlencoded``, not JSON. The webhook
    #: endpoint reads this to skip the JSON nesting scan and the malformed-JSON
    #: 400 — an adapter that got it wrong would make every delivery fail.
    webhook_content = "form"

    # `resolve_connection` stays the base class's None: SPEC §7.1 gives SMS a
    # per-connection URL, so the endpoint already knows which connection this is
    # before an adapter is asked.

    # -- inbound ------------------------------------------------------------

    def verify_webhook(self, request: "HttpRequest", connection: ChannelConnection) -> bool:
        """Constant-time check of ``X-Twilio-Signature`` (SPEC §6.6).

        Fails closed on everything — no token stored, no header, a header that
        is not base64, a URL that matches no candidate — and every one of those
        is indistinguishable from a wrong signature to the caller. A
        distinguishable "malformed header" reply would be a free oracle telling
        an attacker their format was right.
        """
        token = auth_token(connection)
        presented = (request.headers.get(SIGNATURE_HEADER) or "").strip()
        if not token or not presented:
            return False

        params = _post_params(request)
        return any(security.constant_time_equal(presented, sign(token, url, params)) for url in public_urls(request))

    def parse_events(self, request: "HttpRequest", connection: ChannelConnection) -> list[NormalizedEvent]:
        """One Twilio callback becomes at most one normalized event.

        Defensive by contract (SECURITY-BASELINE §2): every value here was typed
        by a stranger or forwarded by a carrier on their behalf. Nothing raises,
        nothing assumes a key exists, everything is length-bounded, and a
        callback we do not understand produces no event rather than a
        half-populated one.

        Twilio uses one URL for two unrelated things, so the first job is to
        tell them apart, and the discriminator is the status: ``received`` is a
        message from a person, anything in :data:`DELIVERY_STATUSES` is a
        receipt about a message from us. A payload carrying both is decided by
        this one rule rather than by which key happens to be read first.
        """
        params = _post_params(request)
        status = (params.get("MessageStatus") or params.get("SmsStatus") or "").strip().casefold()

        if status in DELIVERY_STATUSES:
            return self._delivery_status(connection, params, status)
        if status == INBOUND_STATUS or (not status and params.get("From")):
            # The second half is a deliberate fallback rather than a guess: the
            # signature has already proved this came from Twilio, so a payload
            # with a sender and no status is a shape change on their side, and
            # dropping a real customer message over one is the worse failure.
            return self._inbound(connection, params)

        logger.info("SMS callback on connection %s carried no usable status; ignored.", connection.pk)
        return []

    def _inbound(self, connection: ChannelConnection, params: dict[str, str]) -> list[NormalizedEvent]:
        sender = _address(params.get("From"))
        message_sid = _text(params.get("MessageSid") or params.get("SmsSid"), MAX_PLATFORM_ID_CHARS).strip()
        if not sender:
            # No one to attribute this to. An identity keyed on the empty string
            # would collide with every other empty one.
            logger.info("Inbound SMS on connection %s carried no sender; ignored.", connection.pk)
            return []

        body = _text(params.get("Body"))
        media = _media_urls(params)
        if not body and not media:
            return []

        # The dedup key (SPEC §7.1 step 2). Twilio always sends a MessageSid;
        # a delivery without one falls back to a digest of the payload, which is
        # what ``synthetic_event_id`` documents itself as existing for.
        provider_event_id = message_sid or channels_ingest.synthetic_event_id(params, prefix="sms:")
        # Twilio's form posts carry no trustworthy timestamp for the message
        # itself, and SPEC §8's window is computed from our own clock regardless
        # (``apps.messaging.ingest``: "the clock is ours, not the platform's").
        now = timezone.now()

        message = NormalizedEvent(
            type=EventType.MESSAGE,
            connection=connection,
            platform_user_id=sender,
            provider_event_id=provider_event_id,
            timestamp=now,
            payload=EventPayload(text=body, attachments=media, extra=_extra(params)),
            raw=dict(params),
        )
        if keyword(body) not in OPT_OUT_KEYWORDS:
            return [message]

        # A STOP is **two** events, in this order, and the pairing is the whole
        # of this adapter's opt-out job. ROADMAP contract 3 gives
        # ``opted_out_at`` one write site and it is not here; emitting
        # ``EventType.OPT_OUT`` is what lets ``apps.messaging.ingest`` apply it
        # and ``apps.channels.sms_compliance`` answer it.
        #
        # The message half is not decoration. ``ingest._persist_one`` handles an
        # opt-out by stamping the column and returning **before** it writes a
        # thread row, so an opt-out alone left the conversation showing the
        # confirmation we send with nothing above it explaining why — an agent
        # reading the thread could not see that the contact had asked. Emitting
        # the message first puts the STOP in the thread and updates recency; the
        # opt-out then suppresses the identity in the same delivery, which is
        # what SPEC §21 requires.
        #
        # Their ids differ, or the event log's unique ``(connection,
        # provider_event_id)`` would drop the second as a duplicate delivery.
        # The message keeps the bare ``MessageSid`` so a redelivery still
        # deduplicates against the row it wrote.
        #
        # ``sms_compliance.sms_keywords`` consumes the message half at the
        # ``hard_optout`` stage, so it never reaches trigger matching — without
        # that, a keyword trigger on the word "STOP" would start a flow at
        # somebody who just unsubscribed, which is the exact failure
        # ``stages.opt_out_event`` exists to prevent for the other half.
        return [
            message,
            NormalizedEvent(
                type=EventType.OPT_OUT,
                connection=connection,
                platform_user_id=sender,
                provider_event_id=f"sms:optout:{provider_event_id}",
                timestamp=now,
                payload=EventPayload(text=body, extra=_extra(params)),
                raw=dict(params),
            ),
        ]

    def _delivery_status(
        self,
        connection: ChannelConnection,
        params: dict[str, str],
        status: str,
    ) -> list[NormalizedEvent]:
        """A status callback, in the shape ``apps.messaging.ingest`` reads.

        That module's docstring fixes the convention — ``payload.extra`` carries
        ``provider_message_id``, ``status`` and an optional ``error`` — because
        ``EventPayload`` has no field for "the message this receipt is about"
        and widening it would mean editing another workstream's shipped app.
        """
        message_sid = _text(params.get("MessageSid") or params.get("SmsSid"), MAX_PLATFORM_ID_CHARS).strip()
        if not message_sid:
            logger.info("SMS status callback on connection %s named no message; ignored.", connection.pk)
            return []

        error_code = _text(params.get("ErrorCode"), MAX_EXTRA_CHARS).strip()
        return [
            NormalizedEvent(
                type=EventType.DELIVERY_STATUS,
                connection=connection,
                # A receipt is about a message we sent; there is no inbound
                # sender. ``To`` is the contact, and carrying it keeps the event
                # attributable in the log without implying it created anything.
                platform_user_id=_address(params.get("To")),
                # Several callbacks arrive for one message — queued, sent,
                # delivered — so the SID alone would make all but the first look
                # like a duplicate delivery and be dropped by the event log.
                provider_event_id=f"sms:{message_sid}:{status}",
                timestamp=timezone.now(),
                payload=EventPayload(
                    extra={
                        "provider_message_id": message_sid,
                        "status": DELIVERY_STATUSES[status],
                        "error": error_code,
                    }
                ),
                raw=dict(params),
            )
        ]

    # -- outbound -----------------------------------------------------------

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        """Deliver one message, downgrading it first (SPEC §6.1).

        The downgrade can turn one abstract message into several, and they go in
        order. The result reports the **last** provider id: it is the message the
        contact is looking at, and the one a delivery receipt will reference.

        **A multi-part send is not atomic**, exactly as Telegram's is not: if the
        third of three calls fails, the first two have arrived and the retry
        (SPEC §9.4 keys idempotency on the message *row*, of which there is one)
        sends all three again. The behaviour is duplicate-rather-than-drop, which
        is the right direction for a message a flow author intended to send, and
        it is written down here rather than discovered in production. On SMS it
        also costs money, which is why :func:`wire_calls` joins text blocks
        instead of sending one message each.
        """
        to = _address(getattr(identity, "platform_user_id", ""))
        if not to:
            return SendResult(status=SendStatus.FAILED, error="no_recipient")

        sender = sender_params(connection)
        if not sender:
            # Neither a from-number nor a messaging service. The connect flow
            # refuses to create such a row; a hand-made one fails here rather
            # than at Twilio, where the error text quotes the request.
            return SendResult(status=SendStatus.FAILED, error="no_sender")

        rendered = downgrade(outbound, self.capabilities)
        calls: list[dict[str, Any]] = []
        for message in rendered.messages:
            calls.extend(wire_calls(to, sender, message))
        if not calls:
            # Nothing sendable survived. Reported rather than silently counted
            # as sent, so contract 1's message row says what happened.
            return SendResult(status=SendStatus.FAILED, error="empty_message")

        status_callback = webhook_url(connection)
        provider_message_id = ""
        for payload in calls:
            try:
                result = call(
                    connection,
                    "POST",
                    _account_url(account_sid(connection), "Messages.json"),
                    data={**payload, "StatusCallback": status_callback},
                )
            except APIError as exc:
                self._handle_send_error(connection, to, exc)
                raise
            provider_message_id = _text(result.get("sid"), MAX_PLATFORM_ID_CHARS) or provider_message_id
        return SendResult(status=SendStatus.SENT, provider_message_id=provider_message_id)

    def _handle_send_error(self, connection: ChannelConnection, to: str, exc: APIError) -> None:
        """Turn Twilio's own opt-out into ours, then let the error carry on.

        Twilio tracks opt-outs at its end too — a contact who replied STOP to a
        different application on the same number, or before this workspace
        existed — and answers ``21610`` for every send to them. That means the
        same thing operationally as a Telegram 403: never send here again.

        The adapter does **not** write ``identity.opted_out_at`` itself (ROADMAP
        contract 3). It raises the event the pipeline already knows how to apply
        and hands it to the same dispatch a webhook would, which also lets the
        ``hard_optout`` hook and anything else on the seam see it.
        """
        if exc.code != UNSUBSCRIBED_RECIPIENT_CODE:
            return
        logger.info(
            "Twilio reports connection %s may no longer message a recipient; recording an opt-out.", connection.pk
        )
        now = timezone.now()
        event = NormalizedEvent(
            type=EventType.OPT_OUT,
            connection=connection,
            platform_user_id=to,
            # Timestamped rather than content-only: a contact who opts out, back
            # in and out again is three events, not one duplicate.
            provider_event_id=f"sms:unsubscribed:{to}:{int(now.timestamp())}",
            timestamp=now,
            payload=EventPayload(extra={"reason": "twilio_unsubscribed"}),
        )
        try:
            channels_ingest.process_events(connection, (event,))
        except Exception:
            # The send failure is the thing the caller is waiting to hear about.
            logger.exception("SMS: could not record an opt-out on connection %s.", connection.pk)

    # `send_typing` and `mark_seen` stay the base class's no-ops: SMS has
    # neither, and SPEC §6.1 lists both as "no-op where unsupported".
    #
    # `on_disconnect` stays a no-op too, and that is a decision rather than an
    # omission. Twilio's inbound URL is pasted into its console by an operator,
    # not configured by us the way Telegram's `setWebhook` is, and a number's
    # `SmsUrl` may well be shared with something else on their account. Clearing
    # a setting this product never wrote would be reaching into somebody's
    # Twilio configuration to undo a change they made by hand.


def _post_params(request: "HttpRequest") -> dict[str, str]:
    """This request's form parameters, as the flat dict the signature is over.

    ``request.POST`` is safe to read here even though the endpoint has already
    touched ``request.body``: Django caches the raw bytes and parses the form
    from them, so the order that would raise ``RawPostDataException`` is the
    other one.

    Flattened last-value-wins, which is what ``QueryDict.dict()`` does and what
    every Twilio helper library does with its framework's parsed form. Twilio
    does not send repeated keys; a payload that did would be verified against
    the same flattening the signature check used, so the two cannot disagree.
    """
    try:
        return request.POST.dict()
    except Exception:
        # An unparseable body is not a signature we can compute. The caller
        # treats an empty parameter set as a failed verification.
        logger.info("SMS delivery carried a form body that could not be parsed.")
        return {}


register_adapter(Platform.SMS, TwilioAdapter)
