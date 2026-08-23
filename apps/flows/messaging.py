"""The engine's only door into messaging state — ROADMAP contract 1.

The contract is explicit that L3-B "mutates messaging state **only** through"
L3-A's ``apps/messaging/services.py``: ``send_outbound``,
``upsert_contact_identity``, the conversation operations and
``pause_automation``. This module is that door, and it exists rather than a
scatter of ``from apps.messaging.services import send_outbound`` for two
reasons.

**One import site, resolved late.** L3-A is a parallel sibling (issue #8), so
``apps.messaging`` may not be installed when this app is imported — and this
app's nodes are imported from ``AppConfig.ready()``, where a missing module
would be a boot failure rather than a degraded feature. :func:`_services`
answers "not yet" the way :func:`apps.flows.compat.installed_model` does, and
starts answering for real the moment the app is installed, with no edit at any
call site.

**One seam to fake.** Every engine test that sends a message replaces this
module's functions, not a name imported into six node modules. A fake bound at
the call site is a fake that silently stops being used the day a node imports
the real thing directly; there is nothing to import directly here.

The wrappers are deliberately thin — argument names and order come straight
from the contract, so a signature drift between the two layers is a
``TypeError`` at the seam rather than a wrong-shaped call deep inside a node.
"""

import importlib
import logging
from types import ModuleType
from typing import Any

from django.apps import apps as django_apps

__all__ = [
    "FacadeUnavailableError",
    "assign_conversation",
    "available",
    "bounded_address",
    "bounded_identifier",
    "close_conversation",
    "message_idempotency_key",
    "open_conversation",
    "pause_automation",
    "resolve_identity",
    "send_bucket_tokens",
    "send_outbound",
    "upsert_contact_identity",
]

logger = logging.getLogger(__name__)

_MODULE = "apps.messaging.services"
_APP = "apps.messaging"


class FacadeUnavailableError(RuntimeError):
    """The messaging service facade is not installed in this deployment.

    Reachable in exactly one situation that is not a bug: a tree where L3-A has
    not merged yet. The engine turns it into a named node failure rather than a
    crash, so a flow that reaches a send node reports *why* it stopped.
    """


def _services() -> ModuleType | None:
    """The facade module, or ``None`` while ``apps.messaging`` has not landed."""
    if not django_apps.is_installed(_APP):
        return None
    try:
        return importlib.import_module(_MODULE)
    except ImportError:  # pragma: no cover - installed but without its services module
        logger.exception("%s is installed but %s could not be imported.", _APP, _MODULE)
        return None


def available() -> bool:
    """Whether messaging state can be reached at all right now."""
    return _services() is not None


def _call(name: str, *args: Any, **kwargs: Any) -> Any:
    services = _services()
    if services is None:
        raise FacadeUnavailableError(
            f"{name}() needs the messaging service facade ({_MODULE}, ROADMAP contract 1), "
            f"which this deployment does not have. Flow nodes that send or touch a "
            f"conversation cannot run until issue #8 (L3-A) is installed."
        )
    func = getattr(services, name, None)
    if func is None:
        raise FacadeUnavailableError(
            f"{_MODULE} exposes no {name}(). ROADMAP contract 1 fixes that name; "
            f"a facade missing it is a contract break, not a missing feature."
        )
    return func(*args, **kwargs)


def send_outbound(
    *,
    workspace: Any,
    contact: Any,
    connection: Any,
    outbound: Any,
    source: str,
    idempotency_key: str,
) -> Any:
    """Contract 1's send. Applies compliance, writes the message row, returns it.

    Never raises for a compliance denial — the contract is explicit that a
    denial comes back as a ``Message`` with ``status="failed"`` and a
    machine-readable error code. SPEC §9.5 is what the caller does with that: a
    failed send follows the ``default`` edge onward rather than killing the run.
    """
    return _call(
        "send_outbound",
        workspace=workspace,
        contact=contact,
        connection=connection,
        outbound=outbound,
        source=source,
        idempotency_key=idempotency_key,
    )


def upsert_contact_identity(contact: Any, platform: str, address: str, *, source: str, opt_in: bool) -> Any:
    """Contract 1's identity capture, carrying the SPEC §11.8 consent audit.

    ``source`` and ``opt_in`` are not conveniences: they become ``opt_in_source``
    and ``opt_in_at`` on the identity row, which is the record that says *why*
    this deployment believes it may message this address. The data_collection
    node is the caller that matters here (``source="data_collection"``).
    """
    return _call("upsert_contact_identity", contact, platform, address, source=source, opt_in=opt_in)


def open_conversation(contact: Any, connection: Any = None) -> Any:
    return _call("open_conversation", contact, connection)


def close_conversation(contact: Any, connection: Any = None) -> Any:
    return _call("close_conversation", contact, connection)


def assign_conversation(contact: Any, assignee: Any, connection: Any = None) -> Any:
    return _call("assign_conversation", contact, assignee, connection)


def pause_automation(conversation: Any, until: Any) -> Any:
    return _call("pause_automation", conversation, until)


def message_idempotency_key(execution: Any, node_id: str, attempt: int = 0) -> str:
    """SPEC §9.4's outbound key: ``exec:{execution}:node:{node}:{attempt_bucket}``.

    Lives here rather than in the send node because both sides of contract 1
    depend on it agreeing with itself: L3-B mints the key, L3-A stores it under
    a unique index and skips the provider call on conflict. A key built by two
    different string literals is a duplicate send waiting for a retry.

    ``attempt`` is SPEC's "attempt bucket": 0 inline, and the retry attempt for
    a ``send_retry`` action, so a deliberate retry gets a fresh key while a
    re-run of the same step does not.
    """
    return f"exec:{execution.pk}:node:{node_id}:{attempt}"


# ---------------------------------------------------------------------------
# Inbound routing's reads (issue #11)
# ---------------------------------------------------------------------------
#
# Contract 1 is about *mutating* messaging state, and none of these three do —
# they read. They live here anyway, for the second reason in the module
# docstring: this is the one seam, and a routing module that reached straight
# into ``apps.messaging.identities`` would be a hard import at a new call site
# and a second thing to fake. ``_MODULE`` is services; these name their own.

_IDENTITIES_MODULE = "apps.messaging.identities"
_BUCKETS_MODULE = "apps.messaging.buckets"


def _module(path: str) -> ModuleType | None:
    if not django_apps.is_installed(_APP):
        return None
    try:
        return importlib.import_module(path)
    except ImportError:  # pragma: no cover - installed but without the module
        logger.exception("%s is installed but %s could not be imported.", _APP, path)
        return None


def bounded_address(platform_user_id: Any) -> str:
    """The stored form of a platform user id, so a lookup finds what ingest wrote.

    Bounding is not truncation — over-long values are hashed — so re-deriving it
    locally would be a second implementation that silently stops agreeing.
    Falls back to the raw value only when messaging is absent, which is a tree
    where nothing wrote an identity either.
    """
    identities = _module(_IDENTITIES_MODULE)
    if identities is None:  # pragma: no cover - messaging is installed everywhere
        return str(platform_user_id or "")
    return str(identities.bounded_address(platform_user_id))


def bounded_identifier(value: Any, *, limit: int = 200) -> str:
    """A storable identifier, bounded **without truncation**.

    ``apps.messaging.identities.bounded_key`` hashes an over-long value rather
    than cutting it, and that difference is load-bearing wherever the result
    lands in a unique constraint or is compared against a stored one: two values
    agreeing on their first ``limit`` characters would otherwise silently become
    the same key. Routing needs the same rule for a queued event's ids and for
    the comment guards, so it uses the same function rather than a second
    spelling of it.

    Idempotent — a value already bounded passes through unchanged — so bounding
    on the way into a queue payload and again on the way out is safe.
    """
    identities = _module(_IDENTITIES_MODULE)
    if identities is None:  # pragma: no cover - messaging is installed everywhere
        return str(value or "")[:limit]
    return str(identities.bounded_key(value, limit=limit))


def resolve_identity(connection: Any, platform_user_id: str, *, occurred_at: Any = None) -> Any:
    """Create or refresh the identity behind a platform user id.

    Routing needs this for exactly one case: a comment, which
    ``apps.messaging.ingest`` deliberately persists no identity for. Every other
    inbound type already has one by the time routing runs, and looking one up is
    a read.
    """
    identities = _module(_IDENTITIES_MODULE)
    if identities is None:
        raise FacadeUnavailableError(
            f"resolve_identity() needs {_IDENTITIES_MODULE} (ROADMAP contract 1), which this deployment does not have."
        )
    return identities.resolve_identity(connection, platform_user_id, occurred_at=occurred_at)


def send_bucket_tokens(connection: Any) -> float | None:
    """How many send tokens this connection has, **without touching the row**.

    ``buckets.try_acquire(cost=0.0)`` reports the same number and debits nothing,
    but it gets there through ``SELECT … FOR UPDATE`` and an ``UPDATE`` — so
    every inbound event would take an exclusive lock on the one bucket row for
    its connection before the contact lock was even attempted, and a busy bot's
    events would queue on it inside SPEC §7.1's 1.5-second budget.

    This is advisory and unlocked, which is the right shape for a gate: the
    authoritative debit is still ``send_outbound``'s own non-blocking acquire,
    which SPEC §8 already requires to fall back to the queue when the bucket is
    empty. All this decides is whether to *start* an inline reply on a connection
    that has clearly run out.

    The refill arithmetic mirrors ``buckets._spend`` and reads the rate through
    the same public ``rate_for``/``capacity_for``, so a changed
    ``DEFAULT_SEND_RATE_OVERRIDES`` takes effect here at the same moment it takes
    effect there. ``None`` means messaging is not installed, which the caller
    reads as "do not let a missing app block routing".
    """
    buckets = _module(_BUCKETS_MODULE)
    if buckets is None:  # pragma: no cover - messaging is installed everywhere
        return None

    rate = buckets.rate_for(connection.platform)
    row = (
        buckets.SendBucket.objects.annotate(db_now=buckets.ClockTimestamp())
        .filter(connection=connection)
        .values("tokens", "refilled_at", "db_now")
        .first()
    )
    if row is None:
        # No row yet means nothing has ever been sent on this connection, and
        # the bucket is created full at the first send.
        return buckets.capacity_for(rate)

    elapsed = max(0.0, (row["db_now"] - row["refilled_at"]).total_seconds())
    return float(min(buckets.capacity_for(rate), row["tokens"] + elapsed * rate))
