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

#: Nesting past this is a bomb, not a payload. Meta's deepest real structure is
#: about six levels.
DEFAULT_MAX_JSON_DEPTH = 20

#: How many signature failures from one source before it is banned, and for how
#: long. Small: a legitimate platform never fails a signature check, so any
#: failure at all is either a misconfiguration (which the operator fixes once)
#: or someone guessing.
DEFAULT_SIGNATURE_FAILURE_LIMIT = 10
DEFAULT_SIGNATURE_FAILURE_WINDOW = 300
DEFAULT_SIGNATURE_BAN_SECONDS = 900

_IP_NAMESPACE = "webhook-sig-ip"
_CONNECTION_NAMESPACE = "webhook-sig-conn"


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


def is_banned(request: HttpRequest, connection_id: Any = None) -> bool:
    """True when this source is serving a signature-failure ban.

    One indexed lookup on a primary-key-like unique column, run before any HMAC
    work — a caller who is already banned costs almost nothing.
    """
    now = timezone.now()
    keys = [_ban_key(_IP_NAMESPACE, client_identity(request))]
    if connection_id is not None:
        keys.append(_ban_key(_CONNECTION_NAMESPACE, str(connection_id)))
    return RateLimitCounter.objects.filter(key__in=keys, expires_at__gt=now).exists()


def record_signature_failure(request: HttpRequest, connection_id: Any = None) -> bool:
    """Count one signature failure; return True when it triggered a ban.

    Counted per source **and** per connection. Per source stops one host
    grinding through secrets; per connection stops a distributed attempt at one
    workspace's connection, which per-source counting alone would miss.

    Only failures are counted. A busy, correctly configured connection never
    approaches the limit, which is what keeps this from becoming an accidental
    throughput cap on a live deployment.
    """
    limit = _setting("WEBHOOK_SIGNATURE_FAILURE_LIMIT", DEFAULT_SIGNATURE_FAILURE_LIMIT)
    window = _setting("WEBHOOK_SIGNATURE_FAILURE_WINDOW_SECONDS", DEFAULT_SIGNATURE_FAILURE_WINDOW)

    banned = False
    targets = [(_IP_NAMESPACE, client_identity(request))]
    if connection_id is not None:
        targets.append((_CONNECTION_NAMESPACE, str(connection_id)))

    for namespace, identity in targets:
        if hit(window_key(namespace, identity, window_seconds=window), limit=limit, window_seconds=window):
            _ban(namespace, identity)
            banned = True

    logger.warning(
        "Webhook signature verification failed (path=%s, banned=%s)",
        request.path,
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


def max_json_depth(raw: bytes) -> int:
    """Deepest bracket nesting in ``raw``, without parsing it.

    A linear scan over bytes, string-aware so a ``{`` inside a quoted value does
    not count. Deliberately done **before** ``json.loads``: Python's parser
    recurses, and a deeply nested document is a stack overflow — a crash, not an
    exception you can catch reliably — so the cap has to apply to the input
    rather than to the result.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # closing quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            deepest = max(deepest, depth)
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1
    return deepest


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
