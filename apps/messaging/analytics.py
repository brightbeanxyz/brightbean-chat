"""This app's only door out to the analytics counters (issue #26).

The mirror of :mod:`apps.flows.messaging`, and it exists for the two reasons that
module gives.

**One import site, resolved late.** ``apps.analytics`` sits above this app, so a
module-level ``from apps.analytics.counters import …`` here would invert the
dependency and break a deployment that does not install it. :func:`_counters`
answers "not installed" the way ``apps.flows.compat.installed_model`` does, and
starts answering for real the moment the app is there, with no edit at any call
site.

**One seam to fake.** A test that wants to know whether a send was counted
replaces this module's one function rather than reaching into another app's SQL.

--------------------------------------------------------------------------
Why this is not a second write site
--------------------------------------------------------------------------

ROADMAP contract 3 makes ``automation_paused_until``, ``window_expires_at`` and
``opted_out_at`` single-write-site columns, and
``apps/messaging/tests/test_write_sites.py`` enforces it by scanning the tree.
Nothing here writes a message: :func:`record_status` is called *after* a write
has already landed, and it reads. SPEC §18 asks for counters "from the send
pipeline", and the send pipeline is this app — instrumenting anywhere else would
mean a second path that knows when a message was sent, which is exactly what
``apps/flows/engine/sending.py`` warns against.

Every call site passes the status the row moved **from** as well as the one it
moved **to**, because that is what makes the counters idempotent — see
:func:`apps.analytics.counters.deltas_for` — and it is called only where the
write actually landed. A compare-and-set that lost its race changed nothing and
must count nothing.
"""

import importlib
import logging
from types import ModuleType
from typing import Any

from django.apps import apps as django_apps

__all__ = ["available", "record_status"]

logger = logging.getLogger(__name__)

_MODULE = "apps.analytics.counters"
_APP = "apps.analytics"


def _counters() -> ModuleType | None:
    """The counters module, or ``None`` where analytics is not installed."""
    if not django_apps.is_installed(_APP):
        return None
    try:
        return importlib.import_module(_MODULE)
    except ImportError:  # pragma: no cover - installed but without its counters module
        logger.exception("%s is installed but %s could not be imported.", _APP, _MODULE)
        return None


def available() -> bool:
    """Whether counters can be recorded at all right now."""
    return _counters() is not None


def record_status(message: Any, *, previous: str, current: str) -> None:
    """Record one status transition. Never raises, whatever happens downstream.

    A counter is reporting; a message is the product. An analytics failure that
    propagated out of here would turn a delivered message into a failed one, so
    this swallows — loudly. ``apps.analytics.counters.bump`` already takes its own
    savepoint, so a database error there cannot poison the caller's transaction;
    this catch is for everything else.
    """
    counters = _counters()
    if counters is None:
        return
    try:
        counters.record_message_status(message, previous=previous, current=current)
    except Exception:  # pragma: no cover - defensive; the counter must never cost a send
        logger.exception("Analytics counters failed for message %s", getattr(message, "pk", "?"))
