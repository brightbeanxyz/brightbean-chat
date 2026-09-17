"""Starting a checkout, opening the portal, and the writes that go with them.

Everything here runs on a user-facing POST, so every failure has to end as a page
with a sentence on it rather than a traceback. The one thing that is *not* here
is granting a plan: that only ever happens in :mod:`apps.billing.events`, driven
by a signed webhook. A user can edit a return URL; they cannot forge a signature.
"""

import logging
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.billing import stripe_client
from apps.billing.models import BillingCustomer

logger = logging.getLogger(__name__)

#: The two intervals a caller may ask for. An allowlist rather than a lookup
#: against settings, so an unknown value is refused before anything is read.
INTERVALS = ("monthly", "yearly")

#: Hosts a Stripe-issued URL may point at. Checkout and the portal are on
#: different subdomains and Stripe has changed them before, so this matches the
#: registrable domain rather than a fixed pair of hostnames.
#:
#: Public, because ``config/settings/base.py``'s ``form-action`` allowlist has to
#: cover exactly the same set: a URL this accepts and the policy refuses is a
#: redirect the browser silently drops (issue #115), and
#: ``tests/form_action.py`` holds the two together.
STRIPE_HOST_SUFFIX = ".stripe.com"


class BillingError(Exception):
    """Something a user needs told about, in words they can act on."""


def price_id_for(interval: str) -> str:
    """Map ``monthly``/``yearly`` onto a configured price id.

    **The price never comes from the request.** A form posts an interval and
    this picks the id; a price id taken off a form would let anybody check out
    against any price in the Stripe account, including a one-cent one. That is
    the single security property of the checkout flow, and it lives in these
    four lines.
    """
    if interval not in INTERVALS:
        raise BillingError("Choose a monthly or yearly plan.")
    configured = {
        "monthly": (settings.STRIPE_PRICE_ID_MONTHLY or "").strip(),
        "yearly": (settings.STRIPE_PRICE_ID_YEARLY or "").strip(),
    }[interval]
    if not configured:
        raise BillingError("That plan is not available right now.")
    return configured


def billing_page_url() -> str:
    """The absolute URL of the billing page.

    Built from ``settings.APP_URL`` rather than ``request.build_absolute_uri``:
    the second derives from the Host header, which is caller-supplied, and this
    string is handed to Stripe to redirect a paying customer back to.
    """
    try:
        path = reverse("organizations:billing")
    except NoReverseMatch:
        # The page is owned by another change. Until it lands, come back to the
        # site root rather than refusing to sell anything.
        path = "/"
    return f"{settings.APP_URL.rstrip('/')}{path}"


def get_or_create_customer(organization: Any, *, email: str, name: str) -> BillingCustomer:
    """The organization's ``BillingCustomer``, creating the Stripe customer once.

    **The Stripe call happens outside any transaction**, and that is the whole
    shape of this function. Holding one open across it would pin a pooled
    database connection for up to thirteen seconds whenever Stripe is slow —
    ``apps/channels/views_messenger.py`` documents avoiding exactly that for
    Meta's API, for exactly that reason.

    **The race is settled by the unique constraint, not by a lock.**
    ``select_for_update`` was the obvious guard and it does not work here: the
    row it would lock is the one that does not exist yet, so two first-time
    checkouts both see nothing, both proceed, and the loser's ``save()`` hits the
    one-to-one constraint and 500s. ``get_or_create`` handles that collision
    properly — the loser re-reads the winner's row — and the deterministic
    ``Idempotency-Key`` on the Stripe call means both requests were handed the
    *same* customer anyway, so whichever row survives is correct.
    """
    row = BillingCustomer.objects.filter(organization=organization).first()
    if row is not None and row.stripe_customer_id:
        return row

    customer = stripe_client.create_customer(organization_id=organization.pk, email=email, name=name)
    customer_id = str(getattr(customer, "id", "") or "")
    if not customer_id:
        raise BillingError("We could not start checkout. Try again in a minute.")

    with transaction.atomic():
        row, created = BillingCustomer.objects.get_or_create(
            organization=organization,
            defaults={"stripe_customer_id": customer_id},
        )
        if not created and not row.stripe_customer_id:
            # A row existed without a customer id — a checkout that failed
            # part-way through a previous attempt. Fill it in rather than
            # leaving the organization with a row it can never bill against.
            row.stripe_customer_id = customer_id
            row.save(update_fields=["stripe_customer_id", "updated_at"])
        return row


def start_checkout(organization: Any, *, interval: str, email: str, name: str) -> str:
    """Open a Checkout session and return the URL to send the browser to."""
    price_id = price_id_for(interval)

    # Before get_or_create_customer, which on a first attempt is a live POST to
    # Stripe: a refused attempt should not pay a network round trip to be told
    # to come back later.
    existing = BillingCustomer.objects.filter(organization=organization).first()
    if existing is not None and _checkout_in_flight(existing):
        raise BillingError("A checkout is already open. Finish it, or wait a minute and try again.")

    row = get_or_create_customer(organization, email=email, name=name)

    # Re-checked against the row we are about to stamp, because the read above
    # can predate a concurrent request's stamp. Each session carries its own random
    # idempotency key — it has to, so somebody who abandons one and comes back
    # gets a fresh session rather than the dead one — which means nothing else
    # stops a double-submit, or two admins starting at once, from completing two
    # sessions and creating two subscriptions against the same customer.
    #
    # The window is deliberately short. An abandoned session leaves the flag set
    # and must not lock the organization out for good: after
    # PENDING_CHECKOUT_MINUTES the reconcile job has asked Stripe what is true
    # and cleared it, so the same bound serves both.
    if _checkout_in_flight(row):
        raise BillingError("A checkout is already open. Finish it, or wait a minute and try again.")

    session = stripe_client.create_checkout_session(
        customer_id=row.stripe_customer_id,
        price_id=price_id,
        success_url=_success_url(),
        cancel_url=f"{billing_page_url()}?checkout=cancelled",
        organization_id=organization.pk,
    )
    url = _stripe_url(session)

    # Marks the "being activated" state the page shows if the browser beats the
    # webhook back, and is what reconcile_pending_checkouts looks for when the
    # webhook never arrives at all.
    row.checkout_pending_since = timezone.now()
    row.save(update_fields=["checkout_pending_since", "updated_at"])
    return url


def open_portal(organization: Any) -> str:
    """Open a Customer Portal session and return the URL.

    Raises :class:`BillingError` when the organization has no Stripe customer —
    there is genuinely nothing to manage, and the view turns that into a 404
    rather than a distinguishable message.
    """
    row = BillingCustomer.objects.filter(organization=organization).first()
    if row is None or not row.stripe_customer_id:
        raise BillingError("There is no billing account to manage yet.")

    session = stripe_client.create_portal_session(
        customer_id=row.stripe_customer_id,
        return_url=billing_page_url(),
        configuration_id=(settings.STRIPE_PORTAL_CONFIGURATION_ID or ""),
    )
    return _stripe_url(session)


def _success_url() -> str:
    """Where Stripe sends a successful checkout.

    ``{CHECKOUT_SESSION_ID}`` is **Stripe's** template placeholder, not Python's:
    those braces have to reach Stripe literally, so this is concatenated rather
    than built with ``format`` or an f-string, either of which would eat them.
    A test pins the literal.
    """
    return billing_page_url() + "?checkout=success&session_id={CHECKOUT_SESSION_ID}"


def _stripe_url(session: Any) -> str:
    """The session's URL, refused unless it points at Stripe.

    A cheap open-redirect guard on a value that goes straight into a
    ``Location`` header. Stripe returning something else would mean the account
    or the SDK had been tampered with, which is exactly when a blind redirect is
    most expensive.
    """
    url = str(getattr(session, "url", "") or "")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "stripe.com" or host.endswith(STRIPE_HOST_SUFFIX)):
        logger.error("Stripe returned an unusable redirect URL for host %r", host)
        raise BillingError("We could not start checkout. Try again in a minute.")
    return url


def _checkout_in_flight(row: BillingCustomer) -> bool:
    """Whether a checkout was started recently enough to still be live.

    Bounded by the same window ``apps.billing.housekeeping`` uses to decide a
    pending checkout needs reconciling, so the two can never disagree about
    whether one is still in flight.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.billing.housekeeping import PENDING_CHECKOUT_MINUTES

    if row.checkout_pending_since is None:
        return False
    return timezone.now() - row.checkout_pending_since < timedelta(minutes=PENDING_CHECKOUT_MINUTES)
