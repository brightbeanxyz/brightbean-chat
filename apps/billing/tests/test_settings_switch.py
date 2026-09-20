"""``STRIPE_ENABLED`` is derived at import, so it is tested at import.

The trap this exists for is written out in ``config/settings/base.py``:
django-environ returns a default only when a variable is **unset**, and every
one-click deploy target, Railway's template included, sets an *empty* config
variable for a prompt the operator left blank. So ``STRIPE_SECRET_KEY=`` has to
read as "off". Read as "on with a blank key", it produces a deployment that
offers a Subscribe button and then fails on the first click, which is the worst
of the three possible states.

Settings are computed once per process, so a ``settings`` fixture cannot test
this: overriding ``STRIPE_ENABLED`` afterwards proves nothing about how it was
derived. This boots a real subprocess, the arrangement
``apps/common/tests/test_settings_boot.py`` established for the same reason.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.billing.tests.stripe_support import SECRET_KEY

BASE_DIR = Path(__file__).resolve().parents[3]

_STRIPE_VARS = (
    "STRIPE_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRICE_ID_MONTHLY",
    "STRIPE_PRICE_ID_YEARLY",
    "STRIPE_PORTAL_CONFIGURATION_ID",
)

_PROGRAM = "import django;django.setup();from django.conf import settings;print(settings.STRIPE_ENABLED)"


def _derive(**overrides: str) -> str:
    """Boot settings in a fresh process and report ``STRIPE_ENABLED``."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPE_VARS}
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.test"
    # A developer's local .env would otherwise supply the very variables the
    # cases below are controlling.
    env["DJANGO_ENV_FILE"] = str(BASE_DIR / "does-not-exist.env")
    env.update(overrides)

    result = subprocess.run(  # noqa: S603 - every argument is a literal, shell=False
        [sys.executable, "-c", _PROGRAM],
        env=env,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


_COMPLETE = {
    "STRIPE_SECRET_KEY": SECRET_KEY,
    "STRIPE_PRICE_ID_MONTHLY": "price_m",
    "STRIPE_PRICE_ID_YEARLY": "price_y",
}


class TestTheSwitch:
    def test_unset_is_off(self) -> None:
        """The self-hoster's state, and the default."""
        assert _derive() == "False"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_an_empty_value_is_off_not_on_with_a_blank_key(self, blank: str) -> None:
        """The one-click-deploy trap, which is the whole reason for the .strip()."""
        assert _derive(**{**_COMPLETE, "STRIPE_SECRET_KEY": blank}) == "False"

    @pytest.mark.parametrize("missing", ["STRIPE_PRICE_ID_MONTHLY", "STRIPE_PRICE_ID_YEARLY"])
    def test_a_missing_price_keeps_it_off(self, missing: str) -> None:
        """Half-configured is off. A Subscribe button with no price to sell is
        worse than no button — apps/billing/checks.py warns about it at boot."""
        assert _derive(**{**_COMPLETE, missing: ""}) == "False"

    def test_the_webhook_secret_is_not_part_of_the_switch(self) -> None:
        """Deliberate: the real setup order is keys first, register the endpoint
        second, so the UI has to be able to go live before the webhook does. The
        webhook route gates on its own secret, independently."""
        assert _derive(**_COMPLETE) == "True"

    def test_a_complete_configuration_is_on(self) -> None:
        assert _derive(**_COMPLETE, STRIPE_WEBHOOK_SECRET="whsec_x") == "True"
