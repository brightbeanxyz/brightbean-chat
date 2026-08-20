"""Project-wide pytest fixtures.

Deliberately thin. Studio's conftest builds users, organizations and
memberships; those models arrive with issue #31, and the scaffold's tests must
not pretend they exist.
"""

import pytest


@pytest.fixture
def secret_value() -> str:
    """An opaque high-entropy secret with no recognisable credential shape.

    Deliberately shapeless. An earlier version of this fixture was a
    Telegram-style ``<bot_id>:<secret>`` token, which meant the log-scrubbing
    test passed on that one pattern alone — gutting every key-name rule left it
    green. A value only the surrounding ``token=`` / ``Bearer`` context can
    identify makes the test exercise the rule it claims to.
    """
    return "Zq4tPmXk9BvRnLwCyHsDfGjKaEuT7NbM2VxQ"
