"""Every call this project makes to Stripe, and the seam that makes them testable.

Five calls, no more: create a customer, open a Checkout session, open a Customer
Portal session, read a subscription, and read a customer with its subscriptions
expanded (which only the reconcile job uses). Anything else belongs in the Stripe
dashboard, not in here.

**The seam is :func:`_client`.** A test replaces it with a client whose HTTP
transport is a recording fake, and everything below — parameter construction,
form encoding, response parsing, Stripe's own error mapping — runs for real
against it. That is the arrangement ``apps/channels/providers/telegram.py``
documents for ``httpx.MockTransport``, and it is the reason those adapters' tests
prove something: going through the real code path means a 429 really does become
the SDK's ``RateLimitError`` rather than a stub's idea of one.

**Two things about this module are load-bearing and easy to undo.**

*The API version is pinned in code.* ``STRIPE_API_VERSION`` below is sent on
every request, so payload shapes are fixed by this file rather than by whatever
the Stripe account's default happens to be that week. Unpinned, an account-level
version bump changes response shapes under a running deployment with no
deployment of ours involved.

*The import is lazy.* ``stripe`` is imported inside :func:`_client`, not at
module scope, so a deployment with no Stripe configuration never loads it —
which is what lets ``apps.billing`` be installed unconditionally without costing
a self-hoster anything. Moving the import to the top of the file would work and
would quietly undo that.

**On SSRF.** ``tests/test_ssrf_call_sites.py`` asserts every HTTP request in
``apps/`` leaves through one of two doors, but its AST scan only recognises
``httpx``, and the Stripe SDK speaks ``requests``. So this module is egress that
sweep cannot see, and it is registered in that file's ``NON_HTTP_EGRESS`` map and
in ``docs/security-audit.md`` rather than being left to look like it passed.
What constrains it instead: the base URL is a constant in this module, the only
values ever interpolated into a request are ids this deployment stored or
settings an operator wrote, and **no user-supplied URL reaches Stripe** — the
``success_url`` and ``return_url`` below are built from ``settings.APP_URL`` and
a reversed route, never from anything on the request.
"""

import logging
import secrets
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

#: Pinned deliberately. See the module docstring.
STRIPE_API_VERSION = "2026-08-26.dahlia"

#: Ten seconds with a three-second connect. ``READ_TIMEOUT`` (2s) is SPEC §7.1's
#: inline-send budget and too tight for a payment provider; ``BACKGROUND_TIMEOUT``
#: (30s) is a queue worker's budget and far too long for somebody watching a
#: button spin.
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 10

#: No retries inside the client. A user-facing POST that fails re-renders the
#: page with an error, and retrying inline only makes the spinner longer; the one
#: place a retry belongs is the webhook-driven write, where the queue's own
#: backoff does it properly.
MAX_NETWORK_RETRIES = 0

_client_singleton: Any = None


class StripeUnavailableError(Exception):
    """Stripe could not be reached, or refused the call.

    One exception for every failure, on purpose. Callers render a generic
    message from it and **never** Stripe's own text: a provider's error string
    routinely quotes the request that produced it, and this one would be shown
    in a page. ``apps/messaging/codes.py`` makes the same argument about storing
    a provider's error verbatim.
    """


def _client() -> Any:
    """The Stripe client every call below goes through.

    **This is the test seam.** A test monkeypatches this function to return a
    client built on a recording HTTP transport; see
    ``apps/billing/tests/stripe_support.py``.

    Cached per process rather than per call: the SDK's client holds a connection
    pool, and building one per checkout would throw that away.
    """
    global _client_singleton
    if _client_singleton is None:
        import stripe

        _client_singleton = stripe.StripeClient(
            api_key=settings.STRIPE_SECRET_KEY,
            stripe_version=STRIPE_API_VERSION,
            max_network_retries=MAX_NETWORK_RETRIES,
        )
    return _client_singleton


def reset_client() -> None:
    """Drop the cached client. For tests, and for a settings change in one."""
    global _client_singleton
    _client_singleton = None


def _options(*, idempotency_key: str | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if idempotency_key is not None:
        options["idempotency_key"] = idempotency_key
    return options


def _call(what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one Stripe call, turning every failure into one exception.

    Logs the operation and the Stripe error *class*, never the message. The
    message can echo request content, and this goes to the same log pipeline
    everything else does.
    """
    import stripe

    try:
        return fn(*args, **kwargs)
    except stripe.StripeError as exc:
        logger.warning("Stripe call %s failed: %s", what, type(exc).__name__)
        raise StripeUnavailableError(what) from exc


def create_customer(*, organization_id: Any, email: str, name: str) -> Any:
    """Create the Stripe customer for an organization.

    The idempotency key is **deterministic** — a retry of this exact call, from a
    double-submitted form or a re-run request, returns the customer the first one
    made instead of minting a second. Stripe holds an idempotency key for 24
    hours, which is the window that matters here; the ``select_for_update`` in
    ``apps.billing.services`` is the belt to this one's braces.
    """
    return _call(
        "customers.create",
        _client().v1.customers.create,
        {
            "email": email,
            "name": name,
            # So a human in the Stripe dashboard can tell which tenant a
            # customer is, without a lookup in our database.
            "metadata": {"organization_id": str(organization_id)},
        },
        _options(idempotency_key=f"customer:{organization_id}"),
    )


def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
    organization_id: Any,
) -> Any:
    """Open a hosted Checkout session and return it; ``.url`` is where to send them.

    ``price_id`` comes from settings, never from the request — the caller maps a
    ``monthly``/``yearly`` choice onto one of two configured ids. A price id
    taken off a form would let anybody check out against any price in the
    account, including a one-cent one.

    The organization id travels three ways, and the third is not redundant:
    ``client_reference_id`` and ``metadata`` land on the *session*, so they reach
    the ``checkout.session.completed`` event and nothing after it, while
    ``subscription_data.metadata`` lands on the **subscription**, so every later
    ``customer.subscription.updated`` carries it too.

    A fresh idempotency key per call, unlike ``create_customer``: somebody who
    abandons checkout and comes back must get a new session, and a deterministic
    key would hand them the expired one.
    """
    return _call(
        "checkout.sessions.create",
        _client().v1.checkout.sessions.create,
        {
            "mode": "subscription",
            "customer": customer_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(organization_id),
            "metadata": {"organization_id": str(organization_id)},
            "subscription_data": {"metadata": {"organization_id": str(organization_id)}},
        },
        _options(idempotency_key=f"checkout:{secrets.token_urlsafe(24)}"),
    )


def create_portal_session(*, customer_id: str, return_url: str, configuration_id: str = "") -> Any:
    """Open a Customer Portal session and return it; ``.url`` is where to send them.

    ``configuration_id`` is **omitted entirely when blank**, rather than sent as
    an empty string. Stripe answers an empty ``configuration`` with a 400, while
    omitting it correctly falls back to the account's default configuration —
    the same empty-versus-absent distinction ``config/settings/base.py`` warns
    about for environment variables, arriving here from the other end.
    """
    params: dict[str, Any] = {"customer": customer_id, "return_url": return_url}
    if configuration_id.strip():
        params["configuration"] = configuration_id.strip()
    return _call(
        "billing_portal.sessions.create",
        _client().v1.billing_portal.sessions.create,
        params,
        _options(idempotency_key=f"portal:{secrets.token_urlsafe(24)}"),
    )


def retrieve_subscription(subscription_id: str) -> Any:
    """Read one subscription. Used when an event names one we have not stored."""
    return _call(
        "subscriptions.retrieve",
        _client().v1.subscriptions.retrieve,
        subscription_id,
    )


def retrieve_customer_with_subscriptions(customer_id: str) -> Any:
    """Read a customer with its subscriptions expanded.

    The reconcile job's one call: it answers "what is actually true right now"
    for an organization whose webhook never arrived, which is the failure no
    amount of event-ordering care can fix.
    """
    return _call(
        "customers.retrieve",
        _client().v1.customers.retrieve,
        customer_id,
        {"expand": ["subscriptions"]},
    )
