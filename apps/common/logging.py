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

Coverage is the whole message a handler will emit, not just the format
string: the formatted message, the exception traceback, and the stack info.
Tracebacks matter most — an exception's ``str()`` routinely carries the URL,
connection string or header that caused it, and ``logger.exception`` plus
Django's own ``django.request`` 500 handler are the commonest way a credential
reaches a log at all. Because ``exc_info`` is rendered by the *handler's*
formatter, long after any filter runs, the traceback is formatted here and
cached on ``record.exc_text``, which every stdlib formatter reuses instead of
re-rendering.

One tradeoff worth knowing about: scrubbing has to run on the *formatted*
message, because the key and its value routinely live in different places
("token=%s", secret). So the record's args are folded into ``record.msg`` and
cleared. Anything downstream that groups by the unformatted template — Sentry's
fingerprinting, for one — sees the formatted string instead. Losing a little
grouping fidelity is the right side of that trade when the alternative is
shipping credentials to an error tracker.

The patterns below are deliberately free of nested quantifiers: log messages
carry attacker-controlled content (SECURITY-BASELINE §2), so a regex that
backtracks badly here would be a denial-of-service vector rather than a
performance nit.
"""

import io
import logging
import re
import traceback
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


def _redact_args(args: Any) -> Any:
    """Replace every value in a record's ``args``, preserving its shape.

    Used only when %-formatting failed. Scrubbing the args *individually* is
    not enough there: the key name lives in the template ("api_key=%s") while
    the value lives in the args, so an argument inspected on its own has no
    context to match against and a shapeless secret sails straight through.

    The record cannot be emitted anyway — the handler will hit the same
    formatting error and hand it to ``handleError``, which writes the args to
    stderr — so there is nothing to lose by redacting all of them. The arity is
    preserved, and arity is the part that actually diagnoses the bug.
    """
    if isinstance(args, dict):
        return dict.fromkeys(args, REDACTED)
    if isinstance(args, tuple):
        return (REDACTED,) * len(args)
    return REDACTED


def _format_exception(exc_info: Any) -> str:
    """Render an ``exc_info`` triple exactly as ``logging.Formatter`` would."""
    sio = io.StringIO()
    traceback.print_exception(exc_info[0], exc_info[1], exc_info[2], None, sio)
    text = sio.getvalue()
    sio.close()
    return text.removesuffix("\n")


def _scrub_record(record: logging.LogRecord) -> None:
    """Format, scrub and flatten a record in place.

    Covers the message, the exception traceback and the stack info — every
    part a formatter will concatenate into the emitted line.
    """
    try:
        message = record.getMessage()
    except (TypeError, ValueError):
        # Broken %-formatting. getMessage() will fail again inside the handler,
        # and Handler.handleError writes "Message: %r / Arguments: %s" straight
        # to stderr — so the args have to go too, not just be skipped.
        record.msg = scrub(str(record.msg))
        record.args = _redact_args(record.args)
    else:
        record.msg = scrub(message)
        record.args = ()

    # Exception text: pre-render it so the scrubbed copy is what every
    # formatter emits. Formatter.format() only calls formatException() when
    # exc_text is empty, so filling it in here is what makes this stick.
    if record.exc_info:
        if not record.exc_text:
            try:
                record.exc_text = _format_exception(record.exc_info)
            except Exception:  # noqa: BLE001 - a broken traceback must not break logging
                record.exc_text = "[unformattable traceback]"
        record.exc_text = scrub(record.exc_text)
    elif record.exc_text:
        record.exc_text = scrub(record.exc_text)

    if record.stack_info:
        record.stack_info = scrub(record.stack_info)


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
