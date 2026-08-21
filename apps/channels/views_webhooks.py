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
                                     adapter ships. Cannot be confused with a signature
                                     failure in the logs.
Anything else, including a   200     "Never return 5xx for business-logic failures."
processor blowing up
===========================  ======  ==================================================

CSRF is exempt because there is no session: the signature is the credential.
"""

import json
import logging
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
        return _hub_challenge(request, platform)
    early = _reject_early(request, connection_id=None)
    return early if early is not None else _ingest(request, platform=platform, connection=None)


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

    connection = _connection_by_id(connection_id, platform)
    if connection is None:
        # Same answer as a bad signature, on purpose: a distinguishable "no such
        # connection" would confirm which ids are real (SECURITY-BASELINE §1).
        security.record_signature_failure(request, connection_id)
        return _forbidden()
    return _ingest(request, platform=platform, connection=connection)


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


def _ingest(request: HttpRequest, *, platform: str, connection: ChannelConnection | None) -> HttpResponse:
    """verify → dedup → raw-persist → dispatch (ROADMAP contract 6).

    Callers run :func:`_reject_early` first — it is not repeated here, because
    the ban check is a query and running it twice per request would double the
    cost of the cheapest step.
    """
    try:
        raw_body = request.body
    except (RequestDataTooBig, UnreadablePostError):
        # A body Django itself refused to read, or a client that hung up
        # mid-upload. Either way there is nothing to verify.
        return HttpResponse("Payload too large", status=413)
    if len(raw_body) > security.max_body_bytes():
        return HttpResponse("Payload too large", status=413)

    try:
        adapter = adapter_for(platform)
    except AdapterNotRegisteredError:
        # Our gap, not the caller's. 503 makes the platform retry once the
        # adapter ships, and never counts against anyone's throttle.
        logger.warning("Webhook for %s arrived with no adapter registered", platform)
        return HttpResponse("Channel not available", status=503)

    if connection is None:
        connection = adapter.resolve_connection(request, raw_body)
    if connection is not None and connection.status == ConnectionStatus.DISABLED:
        # Enforced here rather than trusting each adapter's resolve_connection
        # to remember: "disabled" has to mean disabled on every route, and a
        # per-adapter check is one an adapter author can omit without anything
        # noticing until a switched-off channel keeps ingesting.
        connection = None
    if connection is None or not adapter.verify_webhook(request, connection):
        security.record_signature_failure(request, connection.pk if connection else None)
        return _forbidden()

    if security.max_json_depth(raw_body) > security.json_depth_limit():
        return HttpResponse("Bad request", status=400)
    if adapter.webhook_content == "json" and security.parse_json_body(raw_body) is None:
        # Sanctioned by SPEC §7.1 — "malformed payloads per platform
        # requirements" — and correct here: a body that is not the JSON the
        # platform promised is not something a retry will fix, but a 400 is what
        # every platform's own documentation says to send.
        return HttpResponse("Bad request", status=400)

    events = _parse_events(adapter, request, connection)
    _record(connection, events)
    return JsonResponse({"status": "ok"})


def _reject_early(request: HttpRequest, *, connection_id: Any) -> HttpResponse | None:
    """Size and ban checks — everything that must precede real work.

    ``connection_id`` is None on the shared ``/webhooks/<platform>/`` route,
    where the connection is not known until the body has been read: only the
    per-source ban applies there. That is the right split rather than a gap —
    the per-connection counter exists for distributed guessing against a
    connection id an attacker already has, and an id only appears in the URL on
    the per-connection routes.
    """
    if security.body_too_large(request):
        # No body read, no query run. The whole point of checking Content-Length
        # first is that this costs nothing to refuse.
        return HttpResponse("Payload too large", status=413)
    if security.is_banned(request, connection_id):
        response = HttpResponse("Too many requests", status=429)
        response["Retry-After"] = str(security.ban_seconds())
        return response
    return None


def _forbidden() -> HttpResponse:
    """The one 403. Identical for a bad signature and an unknown connection."""
    return HttpResponse("Forbidden", status=403)


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


def _record(connection: ChannelConnection, events: list[NormalizedEvent]) -> None:
    """Dedup, persist, dispatch, and mark the outcome (SPEC §7.1 steps 2–4)."""
    fresh: list[NormalizedEvent] = []
    rows: list[WebhookEventLog] = []
    duplicates = 0

    for event in events:
        row = _log_event(connection, event)
        if row is None:
            duplicates += 1
            continue
        fresh.append(event)
        rows.append(row)

    if duplicates:
        logger.info("Skipped %s duplicate event(s) on connection %s", duplicates, connection.pk)

    ok = ingest.process_events(connection, fresh)

    if rows:
        WebhookEventLog.objects.filter(pk__in=[row.pk for row in rows]).update(
            status=WebhookEventStatus.PROCESSED if ok else WebhookEventStatus.FAILED,
            processed_at=timezone.now(),
            updated_at=timezone.now(),
        )


def _log_event(connection: ChannelConnection, event: NormalizedEvent) -> WebhookEventLog | None:
    """Insert one event-log row, or return None because it is a duplicate.

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
    if not event.provider_event_id:
        logger.warning(
            "Adapter for %s produced an event with no provider_event_id; it cannot be deduplicated. "
            "Use apps.channels.ingest.synthetic_event_id.",
            connection.platform,
        )
        return None
    try:
        with transaction.atomic():
            return WebhookEventLog.objects.create(
                connection=connection,
                platform=connection.platform,
                provider_event_id=security.scrub_nulls(event.provider_event_id)[:200],
                raw=_bounded_raw(event.raw),
                status=WebhookEventStatus.RECEIVED,
            )
    except IntegrityError:
        return None
    except DataError:
        # The database refused the value itself — a NUL byte that survived
        # scrubbing, a numeric out of range, something not yet imagined.
        # scrub_nulls handles the case we know about; this is the backstop that
        # keeps an exotic payload from turning into a 500 on the one endpoint
        # strangers can reach. The event is dropped, loudly.
        logger.exception("Database refused a webhook event on connection %s", connection.pk)
        return None


def _bounded_raw(raw: Any) -> dict[str, Any]:
    """Keep ``webhook_event_log.raw`` from becoming a storage amplifier.

    A 256 KB delivery carrying fifty events would otherwise write 12 MB. The
    marker is deliberately self-describing: an operator reading the log should
    see that the payload was dropped for size, not wonder why it is empty.
    """
    if not isinstance(raw, dict):
        return {}
    try:
        encoded = json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return {"_unserializable": True}
    if len(encoded) > MAX_RAW_BYTES:
        return {"_truncated": True, "_bytes": len(encoded)}
    # jsonb cannot hold \u0000 any more than a text column can hold \x00.
    return security.scrub_nulls(raw)
