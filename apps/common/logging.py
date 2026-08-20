"""Global log scrubbing (SECURITY-BASELINE §5).

Studio has no scrubbing at all; this is new work. The rule it enforces is that
credentials and tokens never appear in logs, error reports or CI output, no
matter which code path happens to format them into a message.

Two installation points, on purpose:

``SecretScrubbingFilter``
    Attached to every handler in ``settings.LOGGING`` (see
    ``config/settings/base.py``). This is the declarative, reviewable half —
    you can read the settings and see that scrubbing is on.

``install_scrubbing_record_factory``
    Called from ``apps.common.apps.CommonConfig.ready()``. A ``logging.Filter``
    only runs on the handlers it is attached to, so anything that adds its own
    handler — pytest's ``caplog``, Sentry's breadcrumb integration, a library
    that calls ``basicConfig`` — would otherwise see the unscrubbed record. The
    record factory runs at record *creation*, before any handler exists in the
    picture, which is the only place that covers all of them.

Scrubbing is idempotent, so a record passing through both is harmless.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "REDACTED",
    "SecretScrubbingFilter",
    "install_scrubbing_record_factory",
    "scrub",
]

REDACTED = "[REDACTED]"

# Key names whose value is a secret whatever it looks like. Matches
# ``token=abc``, ``"api_key": "abc"``, ``client_secret abc`` and friends.
_SECRET_KEY_NAMES = (
    r"tokens?|secrets?|passwords?|passwd|pwd|api[_\-]?keys?|access[_\-]?keys?|"  # noqa: S105 - key *names*
    r"private[_\-]?keys?|client[_\-]?secrets?|refresh[_\-]?tokens?|"
    r"authorization|auth[_\-]?tokens?|credentials?|salt|signature|sig|hmac|"
    r"session[_\-]?keys?|verify[_\-]?tokens?"
)

# HTTP authentication schemes, handled before the generic key=value rule so
# that "Authorization: Bearer <jwt>" redacts the credential rather than the
# word "Bearer".
_AUTH_SCHEMES = r"Bearer|Basic|Token|Digest"

# Ordered: the first pattern that matches a region wins.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization header values: scheme followed by the credential.
    (re.compile(rf"(?i)\b({_AUTH_SCHEMES})\s+[A-Za-z0-9._~+/=\-]{{8,}}"), rf"\1 {REDACTED}"),
    # key = value / key: value. The left boundary breaks on "_" and "-" as well
    # as whitespace, so "access_token=" matches on "token". The key may be
    # quoted (JSON, dict reprs) and so may the value; an unquoted value runs to
    # the first whitespace, comma, semicolon, ampersand or closing bracket. A
    # value that is an auth scheme, or is already redacted, is left alone —
    # which is what keeps scrubbing idempotent.
    (
        re.compile(
            rf"(?i)(?<![A-Za-z0-9])({_SECRET_KEY_NAMES})\b([\"']?\s*[=:]\s*)"
            rf"(?!(?:{_AUTH_SCHEMES})\b|{re.escape(REDACTED)})"
            rf"(\"[^\"]*\"|'[^']*'|[^\s,;&)}}\]]+)",
        ),
        rf"\1\2{REDACTED}",
    ),
    # PEM private key blocks.
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        REDACTED,
    ),
    # JSON Web Tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"), REDACTED),
    # Well-known credential prefixes seen in this project's integrations.
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{6,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # Telegram bot tokens: <bot_id>:<35-char secret>.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"), REDACTED),
)


def scrub(text: str) -> str:
    """Redact token- and secret-shaped values in ``text``.

    Idempotent: scrubbing already-scrubbed text is a no-op.
    """
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_record(record: logging.LogRecord) -> None:
    """Format, scrub and flatten a record in place."""
    try:
        message = record.getMessage()
    except (TypeError, ValueError):
        # Broken %-formatting: scrub the raw template and leave args alone, so
        # a logging bug never becomes a leak.
        record.msg = scrub(str(record.msg))
        return

    record.msg = scrub(message)
    record.args = ()


class SecretScrubbingFilter(logging.Filter):
    """Handler-level scrubber. Never drops records; only rewrites them."""

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


_SCRUBBING_FACTORY_INSTALLED = False


def install_scrubbing_record_factory() -> None:
    """Wrap the global ``logging`` record factory so every record is scrubbed.

    Idempotent — calling it twice does not stack two wrappers.
    """
    global _SCRUBBING_FACTORY_INSTALLED
    if _SCRUBBING_FACTORY_INSTALLED:
        return

    previous_factory: Callable[..., logging.LogRecord] = logging.getLogRecordFactory()

    def scrubbing_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        _scrub_record(record)
        return record

    logging.setLogRecordFactory(scrubbing_record_factory)
    _SCRUBBING_FACTORY_INSTALLED = True
