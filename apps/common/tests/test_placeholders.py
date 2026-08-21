"""Placeholder-secret detection (SECURITY-BASELINE §8)."""

import re
from pathlib import Path

import pytest

from apps.common.placeholders import is_placeholder_secret

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


@pytest.mark.parametrize(
    "value",
    [
        "change-me-to-a-random-string",
        "change-me-to-a-different-random-string",
        "CHANGE-ME",
        "  change-me-to-a-random-string  ",
        "django-insecure-dev-only-do-not-use-in-production",
        "changeme",
        "replace-me",
        "TODO",
        "xxx",
        b"change-me-to-a-random-string",
    ],
)
def test_placeholders_are_recognised(value):
    assert is_placeholder_secret(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        None,
        b"",
        "Zq4tPmXk9BvRnLwCyHsDfGjKaEuT7NbM2VxQ",
        "test-secret-key-not-for-production",  # the suite's own key must pass
        b"\xff\xfe not utf-8",
    ],
)
def test_real_values_are_not_flagged(value):
    assert not is_placeholder_secret(value)


class TestEnvExampleStaysRecognisable:
    """Pins .env.example to the rule instead of duplicating its literals.

    The check knows placeholders by prefix so it does not have to hardcode the
    strings. That only holds while .env.example keeps using those prefixes —
    reword it to "SET_THIS" and production would boot on a published secret
    again with nothing to catch it. This test is what makes the prefix rule
    safe to rely on.
    """

    @pytest.mark.parametrize("name", ["SECRET_KEY", "ENCRYPTION_KEY_SALT"])
    def test_shipped_placeholder_is_rejected(self, name):
        content = ENV_EXAMPLE.read_text()
        match = re.search(rf"^{name}=(.*)$", content, re.MULTILINE)

        assert match, f"{name} is no longer in .env.example"
        value = match.group(1).strip()
        assert value, f"{name} in .env.example is blank; it should carry a placeholder"
        assert is_placeholder_secret(value), (
            f".env.example ships {name}={value!r}, which is_placeholder_secret() does not "
            f"recognise. Production would boot on a value published in this repository. "
            f"Either use one of the PLACEHOLDER_PREFIXES or extend that tuple."
        )
