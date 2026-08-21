"""Recognising placeholder secrets (SECURITY-BASELINE §8).

Pure stdlib and free of Django imports, so ``config/settings/base.py`` can use
it while settings are still being assembled and ``apps.common.checks`` can use
the same rule afterwards.

A blank ``SECRET_KEY`` is caught by the boot check. A *placeholder* one is not:
``.env.example`` ships ``change-me-to-a-random-string``, ``make setup`` copies
that file to ``.env``, and nothing about the resulting value looks empty. A
deployment that follows the README and switches to production settings without
editing it signs every session and derives its field-encryption key from a
string published in this repository — the same failure as a blank key, minus
the error message.

Rather than listing exact strings (which drift the moment ``.env.example`` is
reworded), placeholders are recognised by prefix. Everything this project
ships as a stand-in starts with one of these, and a test asserts the values in
``.env.example`` really are rejected.
"""

PLACEHOLDER_PREFIXES = (
    # Django's own convention, used by startproject and by our DEBUG defaults.
    "django-insecure-",
    # What .env.example tells the operator to replace.
    "change-me",
    # Common hand-written stand-ins.
    "changeme",
    "your-secret",
    "replace-me",
    "todo",
    "xxx",
)

__all__ = ["PLACEHOLDER_PREFIXES", "is_placeholder_secret"]


def is_placeholder_secret(value: str | bytes | None) -> bool:
    """True when ``value`` is a stand-in that must never reach production."""
    if not value:
        return False
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return value.strip().lower().startswith(PLACEHOLDER_PREFIXES)
