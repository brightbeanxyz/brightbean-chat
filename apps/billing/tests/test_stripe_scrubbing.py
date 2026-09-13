"""Stripe credentials never survive into a log line (SECURITY-BASELINE §5).

``apps.common.logging`` already carried patterns for ``sk_/pk_/rk_(live|test)_``
and ``whsec_`` before this app existed — the first registered against Stripe by
name, the second for Svix, which happens to use the same prefix Stripe does. So
this file adds no pattern. It asserts that the ones already there fire against
the shapes this integration actually handles, which is the difference between a
regex somebody wrote once and a regex that is known to work.

That is worth its own file for the reason ``conftest.py``'s ``secret_value``
fixture gives about its own shapelessness: a test using the string ``"secret"``
as a credential proves nothing about a rule written for ``sk_live_…``.

Deliberately **no** patterns are added for ``cus_``, ``sub_``, ``price_`` or
``cs_``. Those are identifiers, not secrets — holding one grants nothing — and
``apps/common/logging.py``'s note on the Twilio auth token explains what an
over-broad pattern costs: it redacts the very ids an operator needs to debug a
failed webhook, in the one log line that could have explained it.
"""

import logging

from apps.billing.tests.stripe_support import SECRET_KEY, WEBHOOK_SECRET
from apps.common.logging import REDACTED, scrub


class TestTheKeysAreRedacted:
    def test_the_secret_key_never_survives_scrubbing(self) -> None:
        assert SECRET_KEY not in scrub(f"Authorization: Bearer {SECRET_KEY}")
        assert SECRET_KEY not in scrub(f"Stripe call failed with {SECRET_KEY}")
        assert REDACTED in scrub(SECRET_KEY)

    def test_the_webhook_signing_secret_never_survives_scrubbing(self) -> None:
        """The one that matters most: it is what makes a forged 'they paid'
        indistinguishable from a real one."""
        assert WEBHOOK_SECRET not in scrub(f"verifying against {WEBHOOK_SECRET}")
        assert REDACTED in scrub(WEBHOOK_SECRET)

    def test_a_key_inside_an_exception_message_is_redacted(self, caplog: object) -> None:
        """The shape a leak actually takes. An SDK exception quotes the request
        that produced it, and this project logs exceptions."""
        logger = logging.getLogger("apps.billing.tests")
        with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
            logger.warning("stripe rejected the call: %s", f"key={SECRET_KEY}")

        captured = "\n".join(record.getMessage() for record in caplog.records)  # type: ignore[attr-defined]
        assert SECRET_KEY not in captured


class TestIdentifiersAreLeftAlone:
    def test_customer_and_subscription_ids_survive(self) -> None:
        """They are not credentials, and redacting them would blind the one log
        line that could explain a mis-handled webhook."""
        line = "event evt_1 for cus_Ab12Cd34Ef56 -> sub_Gh78Ij90Kl12 (price_Mn34Op56)"

        assert scrub(line) == line
