"""The engine's only door into the analytics app (issue #26).

The mirror of :mod:`apps.flows.messaging`, written for the two reasons that
module gives: **one import site, resolved late**, because ``apps.analytics`` sits
above this app and a module-level import would invert the dependency and break a
deployment without it; and **one seam to fake**, because a test that wants to
know whether a message's links were wrapped replaces this function rather than a
name imported into two node modules.

What crosses it is the *mint* side of tracking — rewriting an outbound message's
URL buttons and, for an authored email body, its anchors and its open pixel. The
*count* side is somebody else's seam entirely: counters are written from inside
the messaging facade (:mod:`apps.messaging.analytics`), because that is where a
message reaches a terminal status and ``apps/flows/engine/sending.py`` is
explicit that instrumenting a second path is the thing not to do.

Called from the two places a flow node hands the facade a message: ``deliver``
in :mod:`apps.flows.engine.sending`, and ``send_email``, which
`deliberately does not use deliver <apps/channels/nodes/send_email.py>`_ because
it has to resolve its own connection.
"""

import importlib
import logging
from types import ModuleType
from typing import Any

from django.apps import apps as django_apps

__all__ = ["available", "instrument", "stats"]

logger = logging.getLogger(__name__)

_MODULE = "apps.analytics.tracking"
_SELECTORS_MODULE = "apps.analytics.selectors"
_APP = "apps.analytics"


def _module(path: str) -> ModuleType | None:
    """One of the analytics modules, or ``None`` where the app is not installed."""
    if not django_apps.is_installed(_APP):
        return None
    try:
        return importlib.import_module(path)
    except ImportError:  # pragma: no cover - installed but without that module
        logger.exception("%s is installed but %s could not be imported.", _APP, path)
        return None


def _tracking() -> ModuleType | None:
    return _module(_MODULE)


def available() -> bool:
    """Whether links can be wrapped and counters read at all right now."""
    return _tracking() is not None


def stats(workspace: Any, flow_id: Any, *, days: Any = None) -> dict[str, Any] | None:
    """Totals and per-node counters for one flow, or ``None`` with no analytics.

    ``None`` is what ``apps.flows.api.flow_stats`` renders as ``available:
    false`` — the state its docstring has described since L2-D, now meaning "this
    deployment does not install the analytics app" rather than "L7-A has not
    shipped". The builder distinguishes it from an empty result on purpose
    (``frontend/builder/src/stats/useStats.ts``): painting zeros that look like
    real counters is worse than saying there is nothing behind them.
    """
    selectors = _module(_SELECTORS_MODULE)
    if selectors is None:
        return None
    window = selectors.resolve_range(days)
    return {
        "totals": selectors.flow_totals(workspace, flow_id, window=window),
        "nodes": selectors.flow_node_stats(workspace, flow_id, window=window),
    }


def instrument(
    outbound: Any,
    *,
    execution: Any,
    node_id: str,
    platform: str,
    idempotency_key: str,
) -> Any:
    """Return ``outbound`` with its links wrapped, or unchanged on any failure.

    Never raises. A message whose links could not be rewritten is a message with
    unwrapped links — a lost count — whereas an exception here would be a send
    that did not happen because analytics was unwell. The tracking module already
    returns the message untouched for a preview run; this catch is for everything
    else.
    """
    tracking = _tracking()
    if tracking is None:
        return outbound
    try:
        return tracking.instrument(
            outbound,
            execution=execution,
            node_id=node_id,
            platform=platform,
            idempotency_key=idempotency_key,
        )
    except Exception:  # pragma: no cover - defensive; tracking must never cost a send
        logger.exception("Could not wrap tracking links for execution %s node %s", execution.pk, node_id)
        return outbound
