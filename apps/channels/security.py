"""Webhook request hardening (SECURITY-BASELINE §§2, 4, 7; SPEC §7.1).

These endpoints are the only part of a self-hosted deployment that strangers can
reach without an account, so the order of operations matters as much as the
checks themselves. The endpoint spends effort in increasing order of cost:

1. **Size**, from ``Content-Length`` — no body read, no query.
2. **Ban**, one indexed row read — a source that has been guessing signatures is
   refused before any HMAC is computed.
3. **Signature**, over the **raw body**, before JSON is parsed. Parsing first
   would mean running a parser over an unauthenticated document.
4. **Shape**, with a nesting cap applied to the bytes rather than to a parsed
   object, so a nesting bomb never reaches the parser at all.

Everything here is pure enough to unit test: functions take a request or bytes
and return a decision. The endpoint composes them; the policy lives here.
"""

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

from apps.common.jsonlimits import DEFAULT_MAX_JSON_DEPTH, max_json_depth
from apps.common.net import get_client_ip
from apps.common.ratelimit import RateLimitCounter, hit, window_key

logger = logging.getLogger(__name__)

__all__ = [
    "body_too_large",
    "client_identity",
    "constant_time_equal",
    "ban_seconds",
    "is_banned",
    "json_depth_limit",
    "json_payload",
    "max_json_depth",
    "parse_json_body",
    "record_signature_failure",
    "scrub_nulls",
    "sign_body",
    "verify_signature_header",
]

# --- limits -----------------------------------------------------------------
# Deliberately far below Django's DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB). Real
# webhook deliveries are single-digit kilobytes; Meta batches a few dozen
# events into a payload that still fits comfortably. The cap is what stops an
# unauthenticated caller from making the process allocate megabytes per request.
DEFAULT_MAX_BODY_BYTES = 256 * 1024

#: Nesting past this is a bomb, not a payload. Re-exported from
#: :mod:`apps.common.jsonlimits`, where it sits beside the scanner so the
#: outbound guard (#15) can share both rather than growing a second copy.

#: How many signature failures from one source before it is banned, and for how
#: long. Small: a legitimate platform never fails a signature check, so any
#: failure at all is either a misconfiguration (which the operator fixes once)
#: or someone guessing.
DEFAULT_SIGNATURE_FAILURE_LIMIT = 10
DEFAULT_SIGNATURE_FAILURE_WINDOW = 300
DEFAULT_SIGNATURE_BAN_SECONDS = 900

_IP_NAMESPACE = "webhook-sig-ip"


def _setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    return int(value) if isinstance(value, int | str) else default


def max_body_bytes() -> int:
    return _setting("WEBHOOK_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)


def json_depth_limit() -> int:
    return _setting("WEBHOOK_MAX_JSON_DEPTH", DEFAULT_MAX_JSON_DEPTH)


def ban_seconds() -> int:
    """How long a ban lasts. Also what ``Retry-After`` promises, so the two
    cannot drift when a deployment tunes the setting."""
    return _setting("WEBHOOK_SIGNATURE_BAN_SECONDS", DEFAULT_SIGNATURE_BAN_SECONDS)


# --- 1. size ----------------------------------------------------------------


def body_too_large(request: HttpRequest) -> bool:
    """True when this request's body exceeds the cap.

    Reads ``CONTENT_LENGTH`` only — the point is to decide **before** touching
    ``request.body``, which would pull the whole thing into memory. A request
    with no declared length (chunked transfer) is refused rather than trusted:
    accepting it would mean reading an unbounded body to find out how big it is,
    and no messaging platform sends webhooks chunked.

    An unparseable ``Content-Length`` is refused for the same reason.
    """
    raw = request.META.get("CONTENT_LENGTH")
    if raw in (None, ""):
        return True
    try:
        return int(raw) > max_body_bytes()
    except (TypeError, ValueError):
        return True


# --- 2. ban -----------------------------------------------------------------


def client_identity(request: HttpRequest) -> str:
    """Who to attribute this request to for throttling.

    ``apps.common.net.get_client_ip`` ignores ``X-Forwarded-For`` unless the
    peer is a configured trusted proxy — never read that header directly here,
    or the ban is defeated by a client that sets it.
    """
    return get_client_ip(request) or "unknown"


def _ban_key(namespace: str, identity: str) -> str:
    """A stable key for a ban row — no window number, unlike ``window_key``.

    The ban has an explicit ``expires_at``, so bucketing it into fixed windows
    would make its real duration anywhere between zero and the full period. The
    identity is hashed for the same reason ``apps.common.ratelimit`` hashes
    its own: an address should not sit in plaintext in a table that outlives
    the request.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:ban:{digest}"


def is_banned(request: HttpRequest) -> bool:
    """True when this **source** is serving a signature-failure ban.

    One indexed lookup on a unique column, run before any HMAC work — a caller
    who is already banned costs almost nothing.
    """
    return RateLimitCounter.objects.filter(
        key=_ban_key(_IP_NAMESPACE, client_identity(request)),
        expires_at__gt=timezone.now(),
    ).exists()


def record_signature_failure(request: HttpRequest, connection_id: Any = None) -> bool:
    """Count one signature failure against the **source**; True if it banned it.

    Counted per client address and nothing else. An earlier version also counted
    per connection, and that was backwards: the connection id travels in the
    webhook URL, which the operator pastes into the provider's console, so it is
    known to the provider, sits in provider-side logs and is rendered on the
    settings page. Anyone holding it could spend a handful of wrong signatures
    and get the *victim's* connection refused — a ban keyed on the target rather
    than on whoever is attacking it.

    Nor did it buy anything. What protects a webhook secret is its 256 bits of
    entropy, not a counter: an attacker distributed across enough hosts to evade
    per-source banning is still not going to guess one, and every host they use
    is banned after ``WEBHOOK_SIGNATURE_FAILURE_LIMIT`` tries.

    ``connection_id`` is still accepted, and is logged rather than counted, so
    an operator can see *which* connection is misconfigured.
    """
    limit = _setting("WEBHOOK_SIGNATURE_FAILURE_LIMIT", DEFAULT_SIGNATURE_FAILURE_LIMIT)
    window = _setting("WEBHOOK_SIGNATURE_FAILURE_WINDOW_SECONDS", DEFAULT_SIGNATURE_FAILURE_WINDOW)

    identity = client_identity(request)
    banned = hit(
        window_key(_IP_NAMESPACE, identity, window_seconds=window),
        limit=limit,
        window_seconds=window,
    )
    if banned:
        _ban(_IP_NAMESPACE, identity)

    logger.warning(
        "Webhook signature verification failed (path=%s, connection=%s, source_banned=%s)",
        request.path,
        connection_id,
        banned,
    )
    return banned


def _ban(namespace: str, identity: str) -> None:
    RateLimitCounter.objects.update_or_create(
        key=_ban_key(namespace, identity),
        defaults={"count": 1, "expires_at": timezone.now() + timedelta(seconds=ban_seconds())},
    )


# --- 3. signature -----------------------------------------------------------


def sign_body(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 of the raw body under ``secret``, hex encoded.

    The **raw** body: re-serialising parsed JSON changes key order and
    whitespace, and the resulting digest would never match. This is also why the
    endpoint verifies before it parses.
    """
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    """Compare two strings without leaking their common prefix through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_signature_header(
    *,
    secret: str,
    raw_body: bytes,
    header_value: str | None,
    prefix: str = "sha256=",
) -> bool:
    """Verify a ``sha256=<hex>`` style signature header (Meta's ``X-Hub-Signature-256``).

    Fails closed on everything: no secret, no header, the wrong prefix, a
    non-hex digest. Each of those is indistinguishable from a wrong signature to
    the caller, which is the point — a distinguishable "malformed header" reply
    is a free oracle telling an attacker their format is right.
    """
    if not secret or not header_value:
        return False
    presented = header_value.strip()
    if prefix:
        if not presented.startswith(prefix):
            return False
        presented = presented[len(prefix) :]
    return constant_time_equal(presented, sign_body(secret, raw_body))


# --- 4. shape ---------------------------------------------------------------


def scrub_nulls(value: Any) -> Any:
    """Remove NUL bytes from every string in ``value``, recursively.

    PostgreSQL stores neither ``\x00`` in a text column nor ``\u0000`` in a
    jsonb string, and psycopg raises ``DataError`` rather than truncating. A
    webhook payload carrying one would therefore 500 — which matters twice over:
    an unauthenticated-adjacent 500 is a denial-of-service primitive, and a
    platform that gets a 5xx retries the same body forever until it disables the
    webhook.

    NUL is not meaningful in any messaging payload; it is either a bug upstream
    or someone probing. Dropping it is the only option that keeps the delivery.

    Adapters that persist platform strings of their own should use this at the
    same boundary. It is here rather than in the models because it is a property
    of untrusted input, not of any one column.
    """
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {scrub_nulls(key): scrub_nulls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_nulls(item) for item in value]
    return value


#: Sentinel for "not parsed yet", so a body that legitimately parses to None is
#: not re-parsed on every access.
_UNPARSED = object()


def json_payload(request: HttpRequest) -> dict[str, Any] | None:
    """The request's JSON body, parsed at most once.

    The endpoint parses to decide whether a malformed body is a 400, and the
    adapter parses again to read it — twice through up to
    ``WEBHOOK_MAX_BODY_BYTES`` on the path SPEC §7.1 budgets at 1.5 s of wall
    clock. Caching on the request rather than threading the payload through
    :meth:`Adapter.parse_events` keeps that method's signature exactly as SPEC
    §6.1 writes it, which is the one thing six future adapters will copy.

    Adapters should call this instead of :func:`parse_json_body`.
    """
    cached = getattr(request, "_webhook_json_payload", _UNPARSED)
    if cached is not _UNPARSED:
        return cached  # type: ignore[return-value]
    payload = parse_json_body(request.body)
    request._webhook_json_payload = payload  # type: ignore[attr-defined]
    return payload


def parse_json_body(raw: bytes) -> dict[str, Any] | None:
    """Parse a verified webhook body, or return None.

    None covers every rejection — over-nested, malformed, not an object, wrong
    encoding — because the caller does the same thing with all of them and a
    webhook payload is not a place to raise on. Every platform this project
    speaks to sends a JSON **object** at the top level; a list or a bare scalar
    is not a payload we understand.
    """
    if len(raw) > max_body_bytes():
        return None
    if max_json_depth(raw) > json_depth_limit():
        logger.info("Rejected a webhook payload for excessive JSON nesting")
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None
