"""What the billing page renders. The contract between billing and that page.

One function, returning a plain dict, so the page's view stays an import and a
merged dict rather than growing billing logic — and so a redesign of the page
needs no Python change at all.

The page is deliberately useful on a deployment with **no** Stripe: the usage
figures are read from the same models everything else reads, and "how many people
did we talk to this month, how many seats are in use, how many channels are
connected" is worth answering whether or not anybody is selling anything. What
the unconfigured page does not show is a plan comparison — rendering a Free
column whose limits are fiction on a box that has no limits would be the one
thing the AGPL promise actually forbids.
"""

from typing import Any

from apps.billing import plans
from apps.billing.entitlements import billing_enabled, plan_key, usage_for
from apps.billing.models import BillingCustomer

#: How long after a checkout to keep saying "being activated" before the page
#: stops claiming anything. Comfortably longer than a webhook takes and shorter
#: than the reconcile job's window, so the two never disagree on screen.
ACTIVATING_MINUTES = 15


def billing_context(organization: Any, *, checkout: str = "") -> dict[str, Any]:
    """Everything ``templates/organizations/billing.html`` needs."""
    usage = usage_for(organization)
    row = BillingCustomer.objects.filter(organization=organization).first() if billing_enabled() else None

    return {
        "stripe_enabled": billing_enabled(),
        "plan": plan_key(organization),
        "plans": plans.PLAN_COPY,
        # The paid column's two prices, so the template can render both and let
        # the interval toggle swap them in CSS rather than on the server.
        "paid_prices": plans.PAID_PRICES,
        "yearly_saving": plans.YEARLY_SAVING,
        "usage": usage,
        "usage_rows": _usage_rows(usage),
        "subscription": row,
        # Both gated on the row still entitling. A cancelled subscription can
        # carry a final period_end, and reading that as a renewal told the
        # reader a date that will never come.
        "cancels_on": (
            row.current_period_end if row is not None and row.is_entitled and row.cancel_at_period_end else None
        ),
        "renews_on": (
            row.current_period_end if row is not None and row.is_entitled and not row.cancel_at_period_end else None
        ),
        "can_manage_billing": row is not None and bool(row.stripe_customer_id),
        "checkout_state": _checkout_state(row, checkout),
    }


def _usage_rows(usage: Any) -> list[dict[str, Any]]:
    """The usage figures as rows a template can loop over.

    A list rather than five template branches, so the page renders the same way
    whether a cap is a number or unlimited, and so adding a sixth limit is a
    line here rather than a block there.
    """
    rows = [
        # The glyph names are apps/common/context_processors' existing
        # vocabulary, rendered by templates/partials/_nav_icon.html. Reusing
        # them rather than inventing five more means a nav icon and a usage icon
        # for the same thing can never drift apart.
        ("contacts", "Contacts reached this month", usage.active_contacts, usage.active_contacts_limit),
        ("channels", "Channels connected", usage.channels, usage.channels_limit),
        ("flows", "Active automations", usage.automations, usage.automations_limit),
        ("users", "Users", usage.seats, usage.seats_limit),
        ("grid", "Workspaces", usage.workspaces, usage.workspaces_limit),
    ]
    return [
        {
            "icon": icon,
            "label": label,
            "used": used,
            "limit": limit,
            # Amber at four fifths, red at the cap — but only for a cap you can
            # spend *down*. A limit of one is the plan's shape rather than a
            # state to warn about: the free plan has one user and one workspace
            # by definition, so colouring those would paint two rows red on a
            # brand new account that has done nothing wrong, permanently, which
            # teaches the reader to ignore the colour everywhere else.
            "near_limit": limit is not None and limit > 1 and limit * 0.8 <= used < limit,
            "at_limit": limit is not None and limit > 1 and used >= limit,
        }
        for icon, label, used, limit in rows
    ]


def _checkout_state(row: BillingCustomer | None, checkout: str) -> str:
    """``""``, ``"cancelled"`` or ``"activating"``.

    There is deliberately no ``"success"``: the browser coming back from Stripe
    is not evidence that anything was paid — a return URL is something a reader
    can type. What the page can honestly say is either "you are on the paid
    plan", because the webhook said so, or "this is being activated", because a
    checkout was started and nothing has confirmed it yet.
    """
    if checkout == "cancelled":
        return "cancelled"
    if row is None or row.checkout_pending_since is None or row.is_entitled:
        return ""

    from datetime import timedelta

    from django.utils import timezone

    if timezone.now() - row.checkout_pending_since > timedelta(minutes=ACTIVATING_MINUTES):
        # Past the point where a webhook was ever likely. The reconcile job owns
        # it now, and the page stops claiming something is in flight.
        return ""
    return "activating"
