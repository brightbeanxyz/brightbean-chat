"""The webhook endpoints (SPEC §7.1, SECURITY-BASELINE §§2, 4, 7).

Three routes, one pipeline:

* ``POST /webhooks/<platform>/`` — one URL per platform per deployment. The
  connection is resolved from the payload (Meta) or from a secret in a header
  (Telegram) by the adapter. ``GET`` on the same URL answers Meta's
  ``hub.challenge`` verification.
* ``POST /webhooks/sms/<connection_id>/``
* ``POST /webhooks/email/<provider>/<connection_id>/``

**This is the only unauthenticated write path in the product**, so the pipeline
below is written as a sequence of named steps in cost order and each step says
what it costs. In summary:

    size cap (no I/O) → ban check (one row read) → raw-body signature
    (constant time, before parsing) → shape cap → dedup insert → dispatch → 200

Response discipline, from SPEC §7.1 and the issue:

===========================  ======  ==================================================
Situation                    Status  Why
===========================  ======  ==================================================
Body over the cap            413     Refused before it was read; not a business failure.
Source is banned             429     With ``Retry-After``.
Bad signature                403     The only 403. Also: unknown connection, so the two
                                     are indistinguishable (no existence oracle).
Malformed JSON body          400     SPEC §7.1: "malformed payloads per platform
                                     requirements".
No adapter deployed yet      503     Honest, and makes the platform retry after the
                                     adapter ships. Decided before any connection is
                                     looked up, so every id on that platform gets it —
                                     see _ingest_for_connection on why that ordering is
                                     a security property.
Anything else, including a   200     "Never return 5xx for business-logic failures."
processor blowing up
===========================  ======  ==================================================

CSRF is exempt because there is no session: the signature is the credential.
"""

import hashlib
import json
import logging
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.core.exceptions import RequestDataTooBig
from django.db import DataError, IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, UnreadablePostError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from apps.channels import ingest, security
from apps.channels.events import NormalizedEvent
from apps.channels.models import ChannelConnection, ConnectionStatus, WebhookEventLog, WebhookEventStatus
from apps.channels.providers.base import Adapter
from apps.channels.registry import AdapterNotRegisteredError, adapter_for
from apps.common.platforms import Platform
from apps.credentials.resolution import env_credentials

logger = logging.getLogger(__name__)

__all__ = ["email_webhook", "platform_webhook", "sms_webhook"]

#: Cap on what goes into ``webhook_event_log.raw``. The body cap already bounds
#: a delivery, but one delivery can carry many events and each writes a row, so
#: the log needs its own bound or a legal 256 KB batch becomes 256 KB per event.
MAX_RAW_BYTES = 16 * 1024

#: Meta echoes a numeric challenge. Constraining what we echo keeps an
#: unauthenticated reflected-content endpoint from being useful for anything.
MAX_CHALLENGE_LENGTH = 64

#: The ``provider_event_id`` column's width. Longer ids are hashed, not cut.
MAX_EVENT_ID_LENGTH = 200


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@csrf_exempt
@require_http_methods(["GET", "POST"])
def platform_webhook(request: HttpRequest, platform: str) -> HttpResponse:
    """The shared per-platform URL. Connection resolved by the adapter."""
    if platform not in Platform.values:
        raise Http404("No such platform.")
    if request.method == "GET":
        banned = _banned_response(request)
        return banned if banned is not None else _hub_challenge(request, platform)
    early = _reject_early(request, connection_id=None)
    if early is not None:
        return early
    adapter = _adapter_or_none(platform)
    if adapter is None:
        return _unavailable(platform)
    return _ingest(request, platform=platform, adapter=adapter, connection=None)


@csrf_exempt
@require_POST
def sms_webhook(request: HttpRequest, connection_id: UUID) -> HttpResponse:
    """Twilio's per-number callback (SPEC §6.6)."""
    return _ingest_for_connection(request, connection_id, platform=Platform.SMS)


@csrf_exempt
@require_POST
def email_webhook(request: HttpRequest, provider: str, connection_id: UUID) -> HttpResponse:
    """Bounce and delivery notifications from an email provider (SPEC §6.7).

    ``provider`` (resend / ses / smtp) is part of the URL so a deployment can
    hand each provider its own address and so the adapter knows which body shape
    to expect. It is not a credential and is not used for lookup — the
    connection id is — so an unknown value simply reaches an adapter that will
    not recognise the payload.
    """
    return _ingest_for_connection(request, connection_id, platform=Platform.EMAIL)


# ---------------------------------------------------------------------------
# Meta's GET verification
# ---------------------------------------------------------------------------


def _hub_challenge(request: HttpRequest, platform: str) -> HttpResponse:
    """Answer Meta's subscription check (SPEC §7.1).

    The verify token comes from the deployment's existing ``PLATFORM_<PLATFORM>_<KEY>``
    environment convention — ``PLATFORM_INSTAGRAM_VERIFY_TOKEN`` becomes
    ``{"verify_token": ...}`` in ``settings.PLATFORM_CREDENTIALS_FROM_ENV`` with
    no new settings code. Not configured means **404**, the same answer
    ``/internal/tick`` gives for an unset token: an endpoint that cannot verify
    anything should not advertise that it exists.
    """
    verify_token = str(env_credentials(platform).get("verify_token") or "")
    if not verify_token:
        raise Http404("Webhook verification is not configured for this platform.")

    mode = request.GET.get("hub.mode", "")
    presented = request.GET.get("hub.verify_token", "")
    challenge = request.GET.get("hub.challenge", "")

    if mode != "subscribe" or not security.constant_time_equal(presented, verify_token):
        security.record_signature_failure(request)
        return HttpResponse("Forbidden", status=403)

    # Echoing caller-supplied content, so it is constrained to what Meta
    # actually sends: a short decimal string. nosniff on top, because the one
    # thing worse than reflecting input is reflecting it as a sniffed type.
    if not challenge.isdigit() or len(challenge) > MAX_CHALLENGE_LENGTH:
        return HttpResponse("Forbidden", status=403)
    response = HttpResponse(challenge, content_type="text/plain; charset=utf-8")
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def _ingest_for_connection(request: HttpRequest, connection_id: UUID, *, platform: str) -> HttpResponse:
    """Per-connection route: the id is in the URL, so resolve before verifying."""
    # Cheapest checks first, before the connection lookup: an oversized or
    # banned request must not cost a query.
    early = _reject_early(request, connection_id=connection_id)
    if early is not None:
        return early

    # The adapter is resolved BEFORE the connection, and the order is a
    # security property rather than a performance one. These routes take the
    # connection id in the URL, so "does this id name something real" is a
    # question an unauthenticated caller can ask by watching the status code.
    # Looking the connection up first would answer it: with no adapter
    # deployed, a real id reached the 503 below while an unknown one had
    # already been refused with 403. Resolving the adapter first means this
    # route answers 503 to *every* id while its platform has no adapter, and
    # 403 to every id once it does — indistinguishable either way, which is
    # what lets tests/idor.py waive it from the cross-tenant sweep.
    adapter = _adapter_or_none(platform)
    if adapter is None:
        return _unavailable(platform)

    connection = _connection_by_id(connection_id, platform)
    if connection is None:
        # Same answer as a bad signature, on purpose: a distinguishable "no such
        # connection" would confirm which ids are real (SECURITY-BASELINE §1).
        security.record_signature_failure(request, connection_id)
        return _forbidden()
    return _ingest(request, platform=platform, adapter=adapter, connection=connection)


def _connection_by_id(connection_id: UUID, platform: str) -> ChannelConnection | None:
    """Look a connection up for an unauthenticated request.

    Crosses tenants by necessity — an inbound webhook has no session and
    therefore no workspace — so this is a deliberate, greppable ``.unscoped()``
    (CONTRIBUTING). The URL's UUID is the only key, and a disabled connection is
    treated as absent so switching a channel off actually stops ingestion.
    """
    return (
        ChannelConnection.objects.unscoped()
        .filter(pk=connection_id, platform=platform)
        .exclude(status=ConnectionStatus.DISABLED)
        .first()
    )


def _ingest(
    request: HttpRequest,
    *,
    platform: str,
    adapter: Adapter,
    connection: ChannelConnection | None,
) -> HttpResponse:
    """verify → dedup → raw-persist → dispatch (ROADMAP contract 6).

    Callers run :func:`_reject_early` and resolve the adapter first — neither is
    repeated here. The ban check is a query, and running it twice per request
    would double the cost of the cheapest step; the adapter has to be resolved
    by the caller anyway, so that the per-connection routes can do it before
    they touch the database (see :func:`_ingest_for_connection`).
    """
    try:
        raw_body = request.body
    except (RequestDataTooBig, UnreadablePostError):
        # A body Django itself refused to read, or a client that hung up
        # mid-upload. Either way there is nothing to verify.
        return HttpResponse("Payload too large", status=413)
    if len(raw_body) > security.max_body_bytes():
        return HttpResponse("Payload too large", status=413)

    if connection is None:
        connection = adapter.resolve_connection(request, raw_body)
    if connection is not None and not _usable(connection, platform):
        connection = None
    if connection is None or not adapter.verify_webhook(request, connection):
        security.record_signature_failure(request, connection.pk if connection else None)
        return _forbidden()

    if adapter.webhook_content == "json":
        # Both checks are JSON-specific, and both are a full pass over the body.
        # A form-encoded delivery (Twilio) has no brackets to nest and no JSON to
        # fail on, so running them would be a scan of up to WEBHOOK_MAX_BODY_BYTES
        # to learn nothing.
        if security.max_json_depth(raw_body) > security.json_depth_limit():
            return HttpResponse("Bad request", status=400)
        if security.json_payload(request) is None:
            # Sanctioned by SPEC §7.1 — "malformed payloads per platform
            # requirements" — and correct here: a body that is not the JSON the
            # platform promised is not something a retry will fix, but a 400 is
            # what every platform's own documentation says to send. The parse is
            # cached on the request, so the adapter reads it rather than
            # repeating it.
            return HttpResponse("Bad request", status=400)

    events = _parse_events(adapter, request, connection)
    _record(connection, events)
    return JsonResponse({"status": "ok"})


def _reject_early(request: HttpRequest, *, connection_id: Any) -> HttpResponse | None:
    """Size and ban checks — everything that must precede real work."""
    if security.body_too_large(request):
        # No body read, no query run. The whole point of checking Content-Length
        # first is that this costs nothing to refuse.
        return HttpResponse("Payload too large", status=413)
    return _banned_response(request)


def _banned_response(request: HttpRequest) -> HttpResponse | None:
    """429 when this source is serving a ban, otherwise None.

    Split out from :func:`_reject_early` so the ``hub.challenge`` GET can use it
    too. That path records signature failures for a wrong verify token, and for
    a while it was the one entry point that never *checked* a ban — so a caller
    could be banned by guessing on GET, have every POST refused, and go on
    guessing on GET indefinitely. It cannot share ``_reject_early`` wholesale
    because a GET carries no ``Content-Length`` and the size check refuses that
    by design.
    """
    if not security.is_banned(request):
        return None
    response = HttpResponse("Too many requests", status=429)
    response["Retry-After"] = str(security.ban_seconds())
    return response


def _usable(connection: ChannelConnection, platform: str) -> bool:
    """Whether a resolved connection may be ingested into on this route.

    Both checks live here rather than in each adapter's ``resolve_connection``,
    for the same reason: they have to hold on every route, and a per-adapter
    check is one an adapter author can omit with nothing noticing until a
    switched-off channel keeps ingesting, or until a delivery lands on the wrong
    platform's connection.

    The platform check matters because the resolution helper this framework
    advertises — ``ChannelConnection.resolve_by_webhook_secret`` — matches on the
    secret digest alone, across every platform and every workspace. A connection
    reached through the shared ``/webhooks/<platform>/`` URL must belong to the
    platform whose adapter is about to verify and parse it.
    """
    if connection.status == ConnectionStatus.DISABLED:
        return False
    if connection.platform != platform:
        logger.warning(
            "Adapter for %s resolved a %s connection; refusing it",
            platform,
            connection.platform,
        )
        return False
    return True


def _forbidden() -> HttpResponse:
    """The one 403. Identical for a bad signature and an unknown connection."""
    return HttpResponse("Forbidden", status=403)


def _adapter_or_none(platform: str) -> Adapter | None:
    """An adapter instance, or None because that platform has none yet."""
    try:
        return adapter_for(platform)
    except AdapterNotRegisteredError:
        return None


def _unavailable(platform: str) -> HttpResponse:
    """503 for a platform whose adapter has not shipped.

    Our gap, not the caller's: it makes the platform retry once the adapter
    ships, and it never counts against anyone's throttle. Returned before
    anything about a specific connection has been looked at, so it carries no
    information about which connection ids exist.
    """
    logger.warning("Webhook for %s arrived with no adapter registered", platform)
    return HttpResponse("Channel not available", status=503)


def _parse_events(adapter: Adapter, request: HttpRequest, connection: ChannelConnection) -> list[NormalizedEvent]:
    """Ask the adapter for normalized events, tolerating a parser that blows up.

    ``parse_events`` is contractually defensive, so an exception here is our bug
    rather than a malformed delivery — and answering 5xx for our own bug gets a
    webhook disabled at the provider after enough retries. Log it, drop the
    delivery, return 200.
    """
    try:
        return list(adapter.parse_events(request, connection))
    except Exception:
        logger.exception("Adapter %s failed to parse a delivery on connection %s", adapter.platform, connection.pk)
        return []


class _LogOutcome(StrEnum):
    """Why one event did or did not get a log row.

    Three unrelated things used to be reported as "duplicate", which is the one
    of the three an operator can safely ignore. Someone reading the log while
    messages went missing would conclude the platform was redelivering and never
    find the parser bug or the rejected column value.
    """

    STORED = "stored"
    DUPLICATE = "duplicate"
    NO_ID = "no_id"
    REJECTED = "rejected"


def _record(connection: ChannelConnection, events: list[NormalizedEvent]) -> None:
    """Dedup, persist, dispatch, and mark the outcome (SPEC §7.1 steps 2-4).

    Events are grouped by the connection they name rather than all being
    attributed to the one that carried the delivery. One Meta delivery
    legitimately spans several pages — several ChannelConnections, all signed
    with the same app secret — and ``NormalizedEvent`` carries its own
    ``connection`` precisely so an adapter can say which. Forcing the batch onto
    the delivery-level connection logged and dispatched page B's messages as
    page A's, which on a deployment hosting both is a cross-workspace
    misattribution.

    ``connection`` remains the fallback and the authority: an event naming a
    connection on another platform is dropped by :func:`_event_connection`,
    because the signature was verified against this platform's adapter.
    """
    grouped: dict[Any, list[NormalizedEvent]] = {}
    owners: dict[Any, ChannelConnection] = {}
    for event in events:
        owner = _event_connection(event, connection)
        if owner is None:
            continue
        grouped.setdefault(owner.pk, []).append(event)
        owners[owner.pk] = owner

    for pk, group in grouped.items():
        _record_for(owners[pk], group)


def _event_connection(event: NormalizedEvent, verified: ChannelConnection) -> ChannelConnection | None:
    """Which connection an event belongs to, or None to drop it."""
    owner = getattr(event, "connection", None)
    if owner is None:
        return verified
    if owner.pk == verified.pk:
        return verified
    if owner.platform != verified.platform:
        # The signature was checked against `verified`'s platform adapter; an
        # event claiming a connection on some other platform was not covered by
        # that check.
        logger.warning(
            "Dropped an event naming a %s connection on a %s delivery",
            owner.platform,
            verified.platform,
        )
        return None
    return owner


def _record_for(connection: ChannelConnection, events: list[NormalizedEvent]) -> None:
    """Persist and dispatch one connection's share of a delivery."""
    fresh: list[NormalizedEvent] = []
    rows: list[WebhookEventLog] = []
    counts: dict[str, int] = {}

    for event in events:
        row, outcome = _log_event(connection, event)
        counts[outcome] = counts.get(outcome, 0) + 1
        if row is None:
            continue
        fresh.append(event)
        rows.append(row)

    for outcome, message in (
        (_LogOutcome.DUPLICATE, "Skipped %s duplicate event(s) on connection %s"),
        (_LogOutcome.NO_ID, "Dropped %s event(s) with no usable id on connection %s"),
        (_LogOutcome.REJECTED, "Dropped %s event(s) the database refused on connection %s"),
    ):
        if counts.get(outcome):
            logger.info(message, counts[outcome], connection.pk)

    ok = ingest.process_events(connection, fresh)

    if rows:
        now = timezone.now()
        WebhookEventLog.objects.filter(pk__in=[row.pk for row in rows]).update(
            status=WebhookEventStatus.PROCESSED if ok else WebhookEventStatus.FAILED,
            processed_at=now,
            updated_at=now,
        )


def _dedup_id(raw_id: str) -> str:
    """The value the ``(connection, provider_event_id)`` constraint sees.

    Scrubbed **before** it is tested for emptiness, not after: an id of nothing
    but NUL bytes is truthy, so scrubbing later let it past the "no id" guard and
    stored it as the empty string — after which every later event whose id also
    scrubbed to empty collided with it and was silently discarded as a duplicate.

    Over-long ids are hashed rather than truncated. Truncating narrows the dedup
    key without saying so, and two ids agreeing in their first 200 characters
    would then collide and the second event would vanish. A digest keeps them
    distinct and fits the column.
    """
    scrubbed = security.scrub_nulls(raw_id)
    if len(scrubbed) <= MAX_EVENT_ID_LENGTH:
        return scrubbed
    return f"sha256:{hashlib.sha256(scrubbed.encode('utf-8')).hexdigest()}"


def _log_event(connection: ChannelConnection, event: NormalizedEvent) -> tuple[WebhookEventLog | None, str]:
    """Insert one event-log row; report what happened to it.

    The unique constraint on ``(connection, provider_event_id)`` does the work,
    which is what makes this correct under concurrency: two simultaneous
    deliveries of the same event both try to insert, the database picks a
    winner, and the loser gets an ``IntegrityError`` rather than a second row.
    Checking-then-inserting would have a window between the two where both see
    nothing and both proceed.

    The savepoint is not optional. An ``IntegrityError`` marks the surrounding
    transaction unusable, so without ``atomic()`` here the first duplicate in a
    batch would poison every write after it.
    """
    provider_event_id = _dedup_id(event.provider_event_id)
    if not provider_event_id:
        logger.warning(
            "Adapter for %s produced an event with no usable provider_event_id; it cannot be "
            "deduplicated. Use apps.channels.ingest.synthetic_event_id.",
            connection.platform,
        )
        return None, _LogOutcome.NO_ID
    try:
        with transaction.atomic():
            row = WebhookEventLog.objects.create(
                connection=connection,
                platform=connection.platform,
                provider_event_id=provider_event_id,
                raw=_bounded_raw(event.raw),
                status=WebhookEventStatus.RECEIVED,
            )
        return row, _LogOutcome.STORED
    except IntegrityError:
        return None, _LogOutcome.DUPLICATE
    except (DataError, TypeError, ValueError):
        # The database or the JSON encoder refused the value itself. This is the
        # backstop that keeps an exotic payload from turning into a 500 on the
        # one endpoint strangers can reach. The event is dropped, loudly.
        logger.exception("Database refused a webhook event on connection %s", connection.pk)
        return None, _LogOutcome.REJECTED


def _bounded_raw(raw: Any) -> dict[str, Any]:
    """Keep ``webhook_event_log.raw`` from becoming a storage amplifier.

    A 256 KB delivery carrying fifty events would otherwise write 12 MB. The
    marker is deliberately self-describing: an operator reading the log should
    see that the payload was dropped for size, not wonder why it is empty.
    """
    if not isinstance(raw, dict):
        return {}
    try:
        # No `default=str`. The lenient form serialised values the JSONField's
        # own strict encoder then refused, so a raw payload holding, say, a
        # datetime passed the size check and raised TypeError on the way to the
        # column — past both except clauses in _log_event, and out as a 500.
        # The check now fails exactly where the store would.
        encoded = json.dumps(raw)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    # Bytes, not characters: len() of a str counts code points, so a payload of
    # CJK or emoji message text encodes to three or four times its length and
    # sailed past a cap named for bytes.
    size = len(encoded.encode("utf-8"))
    if size > MAX_RAW_BYTES:
        return {"_truncated": True, "_bytes": size}
    # jsonb cannot hold \u0000 any more than a text column can hold \x00.
    return security.scrub_nulls(raw)
