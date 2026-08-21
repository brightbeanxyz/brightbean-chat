"""Sentry wiring that keeps credentials out of error reports (SECURITY-BASELINE §5).

The baseline requires that tokens never appear "in captured logs, error
reports, admin list displays, or API responses". ``apps.common.logging`` covers
logs. Sentry is the error-report half, and it does **not** go through the
logging pipeline: its Django integration builds an event from the exception
object itself, so neither the global ``LogRecord`` factory nor the handler
filter ever sees it. A traceback whose text this project redacts on stderr
still reaches Sentry in full.

Two independent measures, because they fail differently:

``include_local_variables=False``
    Sentry attaches every stack frame's locals by default. That is precisely
    where a decrypted credential lives — the local holding what an
    ``EncryptedTextField`` just returned — and no amount of string scrubbing
    of *messages* touches it. Turning the capture off is the only complete fix
    for that vector, at the cost of some debugging context.

``before_send`` scrubbing
    Exception values, breadcrumb messages, request data and extra context are
    strings built long before Sentry sees them. Each one goes through the same
    ``scrub()`` the log pipeline uses, so one set of patterns governs both.

``EventScrubber(recursive=True)`` is left on top of that: it removes values by
*key* name (``password``, ``authorization``, …) throughout the event, which
catches structured fields whose values carry no recognisable shape.
"""

from typing import Any

from apps.common.logging import scrub

__all__ = ["configure_sentry", "scrub_event"]

# Bounds the walk over an event Sentry built. Events are not deeply nested;
# this only exists so a cyclic or pathological structure cannot hang the hook
# on the error path, where failing loudly would be its own outage.
_MAX_DEPTH = 20


def _scrub_value(value: Any, depth: int = 0) -> Any:
    """Recursively scrub every string inside a Sentry event payload."""
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    return value


def scrub_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """``before_send`` hook: redact credential-shaped strings anywhere in the event.

    Never drops the event and never raises — an exception here would discard a
    genuine error report, so a scrubbing bug must not also cost observability.
    """
    try:
        return _scrub_value(event)
    except Exception:  # noqa: BLE001 - see docstring
        return event


def configure_sentry(dsn: str, **overrides: Any) -> None:
    """Initialise Sentry with the project's privacy defaults."""
    import sentry_sdk
    from sentry_sdk.scrubber import EventScrubber

    options: dict[str, Any] = {
        "dsn": dsn,
        "traces_sample_rate": 0.1,
        "profiles_sample_rate": 0.1,
        # Never attach cookies, headers or request bodies.
        "send_default_pii": False,
        # See the module docstring: stack locals are where decrypted
        # credentials are, and no string scrubbing reaches them.
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "event_scrubber": EventScrubber(recursive=True),
    }
    options.update(overrides)
    sentry_sdk.init(**options)
