"""A boot-time warning for half-configured Stripe.

The failure this catches is quiet and expensive: a deployment with a secret key
but no price id offers a Subscribe button that cannot work, and one with no
webhook secret takes payments it will never hear about — the customer is charged
and stays on the free plan until somebody notices. Both are configuration
mistakes, and both are invisible until the first person tries to pay.

Warnings rather than errors, deliberately. ``manage.py check`` runs in CI and in
every container start, and turning a partly-filled Stripe configuration into a
hard boot failure would mean an operator midway through setting it up cannot
start the app to finish setting it up. The real setup order *is* keys first,
endpoint second.
"""

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Tags, Warning, register


@register(Tags.compatibility)
def check_stripe_configuration(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Warn when Stripe is partly configured."""
    secret = (getattr(settings, "STRIPE_SECRET_KEY", "") or "").strip()
    if not secret:
        # Not configured at all is the supported, documented state: SPEC §1.1's
        # one tier with everything available. Nothing to say about it.
        return []

    messages: list[CheckMessage] = []
    missing = [
        name
        for name in ("STRIPE_PRICE_ID_MONTHLY", "STRIPE_PRICE_ID_YEARLY")
        if not (getattr(settings, name, "") or "").strip()
    ]
    if missing:
        messages.append(
            Warning(
                f"STRIPE_SECRET_KEY is set but {', '.join(missing)} is empty, so billing stays off.",
                hint=(
                    "STRIPE_ENABLED is derived from the secret key and both price ids. Until every "
                    "one of them is set, every organization reads as unlimited and the checkout "
                    "routes 404 — which is the correct off state, but probably not the intended one "
                    "on a deployment that has a secret key."
                ),
                id="billing.W001",
            )
        )
    if not (getattr(settings, "STRIPE_WEBHOOK_SECRET", "") or "").strip():
        messages.append(
            Warning(
                "STRIPE_SECRET_KEY is set but STRIPE_WEBHOOK_SECRET is empty, so /webhooks/stripe/ answers 404.",
                hint=(
                    "Nothing will hear back from Stripe: a customer can complete checkout and stay "
                    "on the free plan. Register the endpoint in the Stripe dashboard and set its "
                    "signing secret. See docs/billing.md."
                ),
                id="billing.W002",
            )
        )
    return messages
