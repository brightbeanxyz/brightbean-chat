"""Project-wide pytest fixtures.

Deliberately thin. Studio's conftest builds users, organizations and
memberships; those models arrive with issue #31, and the scaffold's tests must
not pretend they exist.
"""

import pytest


@pytest.fixture
def secret_value() -> str:
    """A token-shaped string for encryption and log-scrubbing tests.

    Shaped like a real credential (a Telegram bot token) so the scrubbing tests
    exercise the same patterns production traffic would.
    """
    return "8123456789:AAH-thisIsNotARealTelegramBotToken-0123456"
