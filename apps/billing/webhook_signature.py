"""Verifying Stripe's ``Stripe-Signature`` header, and nothing else.

Its own module so it can be unit-tested without a request, a database or a view —
this is the one piece of the integration where a mistake is silent and total: a
receiver that accepts an unsigned body will grant paid plans to anyone who can
POST, and every functional test still passes.

**Why the SDK does this rather than this file.** Stripe signs
``"{timestamp}.{raw_body}"`` and sends ``t=…,v1=…`` with one ``v1`` per active
secret. ``apps.common.signing.sign_webhook`` already mints that exact timestamped
shape for our *outbound* deliveries, so the primitives were here — but the
grammar has enough corners (multiple signatures during a rotation, a legacy
``v0`` scheme to ignore, a tolerance to enforce both ways) that hand-rolling it
would be writing new crypto-adjacent parsing on the one endpoint where a mistake
is indistinguishable from a customer paying. ``stripe.WebhookSignature`` is the
implementation Stripe tests, and using it is the argument for taking the SDK at
all (``requirements.txt``).

**The order matters and is asserted by a test.** The signature is checked against
the raw bytes *before* anything parses them. Re-serialising parsed JSON changes
key order and whitespace, so a digest over that could never match; and parsing
first would also mean a malformed body could be distinguished from a
wrongly-signed one by its response, which is the oracle
``apps.channels.security.verify_signature_header`` refuses to give.

**Every rejection is one exception with no detail.** Absent header, malformed
header, wrong digest, stale timestamp and future timestamp all raise the same
:class:`SignatureRejectedError`, and the view answers all of them 403. A caller learns
whether they hold the secret and nothing else.
"""

import json
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_SECONDS = 300


class SignatureRejectedError(Exception):
    """The body did not carry a valid, current signature. Deliberately bare."""


def tolerance_seconds() -> int:
    """How old a signed delivery may be.

    This is the **real** replay window. The ``StripeEventLog`` unique constraint
    is a second bound, but its window is a retention period measured in days;
    this one is measured in minutes and is what makes a captured request
    unusable.
    """
    return int(getattr(settings, "STRIPE_WEBHOOK_TOLERANCE_SECONDS", DEFAULT_TOLERANCE_SECONDS))


def verified_event(*, raw_body: bytes, header: str | None, secret: str) -> Any:
    """Return the event as a plain dict, or raise :class:`SignatureRejectedError`.

    ``json.loads`` runs only after ``verify_header`` has returned, so the bytes
    being parsed are bytes Stripe signed.

    A plain dict rather than the SDK's ``Event`` object: every consumer of this
    reads a handful of keys, a dict is what goes into ``StripeEventLog.raw``
    unchanged, and the SDK's object needs an API requestor threaded through it
    for no benefit here.
    """
    import stripe

    try:
        stripe.WebhookSignature.verify_header(raw_body, header, secret, tolerance_seconds())
    except stripe.SignatureVerificationError as exc:
        # Logged at debug and without the header: at INFO this is a free
        # log-flooding surface for anyone who can reach the endpoint.
        logger.debug("Rejected a Stripe webhook signature: %s", type(exc).__name__)
        raise SignatureRejectedError from exc
    except Exception as exc:  # noqa: BLE001 - a malformed header must not 500
        # verify_header raises ValueError for some shapes rather than its own
        # error type. Anything that is not a clean pass is a rejection, and it
        # answers exactly like a wrong digest.
        logger.debug("Rejected a malformed Stripe signature header: %s", type(exc).__name__)
        raise SignatureRejectedError from exc

    if _is_future_dated(header):
        # Stripe's verify_header bounds only how OLD a delivery may be — a
        # timestamp in the future passes it. Minting one needs the signing
        # secret, so this is not a hole an outsider can reach; it is enforced
        # anyway because the status table claims a symmetric window, and a
        # documented bound that is not actually applied is worse than no claim.
        # It also stops a clock that has jumped forward from writing deliveries
        # that stay valid long after they should have expired.
        logger.debug("Rejected a future-dated Stripe webhook signature")
        raise SignatureRejectedError

    try:
        return json.loads(raw_body)
    except ValueError as exc:
        # Verified but unparseable. Only somebody holding the signing secret can
        # get here, so this is a bug or a Stripe change, not an attack — the
        # view answers 400 and the distinction costs nothing.
        raise ValueError("Stripe sent a verified body that is not JSON") from exc


def _is_future_dated(header: str | None) -> bool:
    """Whether the header's ``t`` is further ahead than the tolerance allows.

    Only ever called on a header ``verify_header`` has already accepted, so the
    grammar is known good and an unparseable ``t`` here means the SDK changed
    shape — which reads as "not future dated" rather than as a refusal, because
    the signature has already been proven.
    """
    import time

    for part in (header or "").split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            try:
                return int(value) > time.time() + tolerance_seconds()
            except ValueError:
                return False
    return False
