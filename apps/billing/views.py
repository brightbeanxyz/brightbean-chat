"""The two POSTs: start a subscription, and manage an existing one.

Both are org-tier and gated with ``@require_org_role("admin")`` rather than a
``PERMISSION_KEYS`` entry. That is the ``apps/api/views_keys.py`` precedent
exactly — API keys are org-tier and gate the same way — and it is deliberate
rather than lazy: adding ``manage_billing`` to ``PERMISSION_KEYS`` would
propagate it into ``SCOPE_PERMISSIONS`` in ``apps/api/auth.py`` and hand every
bearer token a scope over payment operations. Nobody asked for an API that can
start a subscription.

Decorator order is the house stack — ``login_required`` outermost,
``require_POST`` innermost — so a cross-tenant GET answers 404 rather than 405,
which would confirm the route exists.

CSRF is **on** for both. The webhook is the only ``csrf_exempt`` thing in this
app, and it is exempt because Stripe holds no cookie.

Both 404 when Stripe is unconfigured. An endpoint that cannot charge anybody
should not advertise that it exists — the ``/internal/tick`` answer, and the same
reasoning ``_hub_challenge`` spells out.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from apps.billing import services
from apps.billing.entitlements import billing_enabled, is_paid
from apps.billing.stripe_client import StripeUnavailableError
from apps.members.decorators import require_org_role
from apps.members.requests import OrgRequest

logger = logging.getLogger(__name__)


class HttpResponseSeeOther(HttpResponseRedirect):
    """303, not 302.

    After a POST, 303 tells the browser to GET the target — which kills the
    "resubmit this form?" prompt on the back button, and means a bounce off
    Stripe does not replay the checkout POST.
    """

    status_code = 303


@login_required
@require_org_role("admin")
@require_POST
def checkout(request: OrgRequest) -> HttpResponse:
    """Start a subscription and hand the browser to Stripe's hosted Checkout.

    A server-side redirect rather than a form posting straight at Stripe: CSP is
    ``form-action 'self'``, so the direct form would be blocked outright. A
    top-level navigation is unrestricted by any directive in force, which is why
    this integration needs **no CSP change at all**.
    """
    _require_configured()

    if is_paid(request.org):
        # Already subscribed. Sending them through checkout again would create a
        # second subscription against the same customer.
        messages.info(request, "You are already on the paid plan.")
        return redirect(services.billing_page_url())

    interval = (request.POST.get("interval") or "").strip()
    try:
        url = services.start_checkout(
            request.org,
            interval=interval,
            email=request.user.email,
            name=request.org.name,
        )
    except services.BillingError as exc:
        messages.error(request, str(exc))
        return redirect(services.billing_page_url())
    except StripeUnavailableError:
        # Never Stripe's own message: a provider's error text routinely quotes
        # the request that produced it, and this is rendered in a page.
        messages.error(request, "We could not start checkout. Try again in a minute.")
        return redirect(services.billing_page_url())

    return HttpResponseSeeOther(url)


@login_required
@require_org_role("admin")
@require_POST
def portal(request: OrgRequest) -> HttpResponse:
    """Hand the browser to Stripe's Customer Portal.

    Opens the custom portal configuration named by
    ``STRIPE_PORTAL_CONFIGURATION_ID`` when one is set; see
    ``stripe_client.create_portal_session`` on why a blank one is omitted rather
    than sent empty.
    """
    _require_configured()

    try:
        url = services.open_portal(request.org)
    except services.BillingError:
        # Nothing to manage. 404 rather than a message, so this answers exactly
        # like a route that was never there.
        raise Http404("There is no billing account to manage.") from None
    except StripeUnavailableError:
        messages.error(request, "We could not open the billing portal. Try again in a minute.")
        return redirect(services.billing_page_url())

    return HttpResponseSeeOther(url)


def _require_configured() -> None:
    if not billing_enabled():
        raise Http404("Billing is not configured on this deployment.")
