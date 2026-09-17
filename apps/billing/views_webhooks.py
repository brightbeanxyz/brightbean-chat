"""Stripe's webhook, the only place a subscription's state is written.

Everything about a subscription — that it exists, what it costs, whether it is
live — arrives here. The browser's return from Checkout is decoration; this is
the authority. That is deliberate: a return URL is something a user can edit, and
a signed webhook is not.

**The pipeline is cost-ordered**, the same order and for the same reasons as
``apps/channels/views_webhooks.py``:

    secret configured? -> size cap (no I/O) -> ban check (one row read) ->
    raw-body signature (constant time, before parsing) -> dedup insert ->
    dispatch -> 200

===============================================  ======
Situation                                        Status
===============================================  ======
``STRIPE_WEBHOOK_SECRET`` unset                  404
Body over the cap                                413
Source is serving a ban                          429 + ``Retry-After``
Signature absent, malformed, wrong, or stale     403
Verified body is not usable JSON                 400
Duplicate event, unknown type, handler raised    200
Handled                                          200
===============================================  ======

**Three of those deserve saying out loud.**

*404 when unconfigured* is the ``/internal/tick`` answer, and Meta's
``_hub_challenge`` states the rule: an endpoint that cannot verify anything
should not advertise that it exists.

*One 403 for four different failures.* Absent, malformed, wrong and stale are
indistinguishable in the response. ``security.verify_signature_header``'s
docstring makes the argument — a separate "malformed header" reply is a free
oracle telling an attacker their format is right and only their secret is wrong.

*200 when a handler raises.* SPEC §7.1 forbids a 5xx for a business-logic
failure, and here it is worse than a style violation: Stripe retries a delivery
until it gets a 2xx and disables an endpoint that keeps failing, so one poison
event answered 500 takes down billing for every customer. The row is marked
``failed``, the exception is logged, and ``reconcile_pending_checkouts`` is what
actually repairs the state.

The ban bookkeeping is shared with the platform webhooks on purpose:
``security.record_signature_failure`` and ``is_banned`` put Stripe in the same
per-IP pool, so there is one throttle policy and one place to audit rather than
two that drift.
"""

import json
import logging

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.billing import events
from apps.billing.webhook_signature import SignatureRejectedError, verified_event
from apps.channels import security

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    """Receive one Stripe event.

    ``csrf_exempt`` because there is no session to protect: Stripe holds no
    cookie, and the signature over the raw body *is* the credential. That is the
    same reasoning every inbound platform webhook carries.
    """
    secret = _webhook_secret()
    if not secret:
        raise Http404("Stripe webhooks are not configured.")

    if security.body_too_large(request):
        # Content-Length only. No body read, no query run — refusing this must
        # cost nothing, or the cap becomes its own denial-of-service surface.
        return HttpResponse("Payload too large", status=413)

    if security.is_banned(request):
        response = HttpResponse("Too many requests", status=429)
        response["Retry-After"] = str(security.ban_seconds())
        return response

    # Verify before parsing. Re-serialising parsed JSON changes key order and
    # whitespace, so a digest taken over that would never match the one Stripe
    # computed — which is why the raw bytes are what gets signed and checked.
    try:
        event = verified_event(raw_body=request.body, header=request.META.get("HTTP_STRIPE_SIGNATURE"), secret=secret)
    except SignatureRejectedError:
        security.record_signature_failure(request)
        return HttpResponse("Forbidden", status=403)
    except ValueError:
        # Verified, but not JSON. Only somebody holding the signing secret can
        # reach this, so no signature failure is recorded against the source —
        # banning the one caller that proved it holds the secret would be
        # exactly backwards.
        logger.warning("Stripe sent a verified body that is not JSON")
        return HttpResponse("Bad request", status=400)

    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]:
        # A verified body that is not an event. Only somebody holding the
        # signing secret can reach this, so it is a bug or a Stripe change
        # rather than an attack — and 400 says so without inviting a retry.
        return HttpResponse("Bad request", status=400)

    try:
        events.handle(event)
    except Exception:  # noqa: BLE001 - see the module docstring: a 5xx here disables the endpoint
        logger.exception("Stripe event %s (%s) could not be handled", event.get("id"), event.get("type"))

    return JsonResponse({"status": "ok"})


def _webhook_secret() -> str:
    from django.conf import settings

    return (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip()


def _json_bytes(payload: object) -> bytes:
    """Only used by tests, kept here so the encoding matches what is verified."""
    return json.dumps(payload, separators=(",", ":")).encode()
