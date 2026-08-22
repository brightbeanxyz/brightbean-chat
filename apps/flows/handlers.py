"""The three queue action types the engine owns (SPEC §5, §9.3, §15).

``start_flow``, ``resume_execution`` and ``followup_timer`` — the names
``apps.queueing.models.ActionType`` already reserves for this issue. Registration
is an import side effect of ``FlowsConfig.ready()``, which is the pattern
:mod:`apps.queueing.registry`'s docstring writes out.

Three things the queue guarantees, so nothing here re-does them:

* the handler runs **inside a transaction that already holds the contact
  advisory lock** when the row names a contact (``apps.queueing.worker``). The
  engine takes the lock again anyway — advisory locks are counted per session,
  so it is free — because the engine's entry points must be correct when called
  from the inline path too;
* raising retries the row on SPEC §15's backoff ladder, up to ``max_attempts``;
* returning normally marks it done, in the same transaction as the work.

Which is why the interesting decision in this module is the *opposite* of
raising. A trigger can name a flow somebody has since unpublished, and a timer
can fire for an execution a reply already resumed. Neither is retriable: five
attempts over six hours cannot make an unpublished flow publishable, and the
row's ``last_error`` would file a normal race under "failed". So
:class:`~apps.flows.engine.FlowNotRunnableError` and a missing execution are
logged and swallowed; everything else propagates.
"""

import logging
from typing import Any
from uuid import UUID

from apps.contacts.models import Contact
from apps.flows.engine import FlowNotRunnableError, resume_execution, start_flow
from apps.flows.models import Flow, FlowExecution, FlowVersion, StartedBy
from apps.queueing.models import ActionType, ScheduledAction
from apps.queueing.registry import register_handler

__all__ = ["handle_followup_timer", "handle_resume_execution", "handle_start_flow"]

logger = logging.getLogger(__name__)


@register_handler(ActionType.START_FLOW)
def handle_start_flow(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Start a flow for a contact.

    Payload: ``contact_id``, ``flow_id``, optional ``flow_version_id``,
    ``connection_id``, ``variables`` and ``started_by``.

    Everything is re-resolved from ids inside the workspace that owns the row.
    A payload is a document that has been sitting in a table — possibly for
    hours, possibly written by a caller in another layer — so treating its ids
    as ids and not as trusted objects is what keeps a scheduled action from
    reaching across tenants.
    """
    workspace_id = action.workspace_id
    contact = _scoped(Contact, workspace_id, payload.get("contact_id"))
    flow = _scoped(Flow, workspace_id, payload.get("flow_id"))
    if contact is None or flow is None:
        logger.warning("start_flow action %s names a contact or flow that is gone; dropping it.", action.pk)
        return

    version = _requested_version(payload, flow, action)
    if version is _UNRESOLVED:
        return
    connection = _connection(workspace_id, payload.get("connection_id"))
    variables = payload.get("variables")

    try:
        start_flow(
            contact,
            flow,
            started_by=str(payload.get("started_by") or StartedBy.API),
            variables=variables if isinstance(variables, dict) else None,
            flow_version=version,
            connection=connection,
        )
    except FlowNotRunnableError as exc:
        # Narrow on purpose. The two permanent argument faults start_flow can
        # raise are both ValueErrors, but so is anything a node, the messaging
        # facade or a signal receiver might raise mid-run — and swallowing one
        # of those would mark the row done and lose the work the queue promised
        # to retry. Both faults are checked above instead, where they can be
        # told apart from a genuine runtime failure.
        logger.warning("start_flow action %s cannot run: %s", action.pk, exc)


#: Returned by :func:`_requested_version` when the payload asked for a version
#: that cannot be resolved. Distinct from ``None``, which means "no version was
#: asked for, use the published one" — conflating the two is what would let a
#: stale preview action send production content.
_UNRESOLVED = object()


def _requested_version(payload: dict[str, Any], flow: Flow, action: ScheduledAction) -> Any:
    """The explicit version this payload names, ``None`` for "the published one".

    A ``flow_version_id`` that is malformed, deleted, or a version of some other
    flow must **drop the action**, not fall back. The caller that supplies one is
    #12's draft preview, and quietly running the published graph instead would
    send a contact the live content while somebody was testing an unpublished
    draft at them.
    """
    raw = payload.get("flow_version_id")
    if not raw:
        return None

    version = _scoped(FlowVersion, flow.workspace_id, raw)
    if version is None or version.flow_id != flow.pk:
        logger.warning(
            "start_flow action %s names flow version %r, which is not a version of flow %s in this "
            "workspace; dropping it rather than running the published graph instead.",
            action.pk,
            raw,
            flow.pk,
        )
        return _UNRESOLVED
    return version


@register_handler(ActionType.RESUME_EXECUTION)
def handle_resume_execution(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Wake a waiting execution — a smart delay expiring, or the inline path's fallback.

    Payload: ``execution_id``, optional ``handle`` (default ``default``) and
    ``token``.
    """
    _resume(payload, action, default_handle="default")


@register_handler(ActionType.FOLLOWUP_TIMER)
def handle_followup_timer(payload: dict[str, Any], action: ScheduledAction) -> None:
    """A wait's deadline (SPEC §11.1's ``followup``): take the ``timeout`` branch.

    Identical machinery to ``resume_execution`` and a separate action type
    anyway, because the two are separately cancellable and separately visible in
    the admin — "why did this contact get the timeout message" is a question
    somebody asks about a specific row.
    """
    _resume(payload, action, default_handle="timeout")


def _resume(payload: dict[str, Any], action: ScheduledAction, *, default_handle: str) -> None:
    execution = _scoped(FlowExecution, action.workspace_id, payload.get("execution_id"))
    if execution is None:
        logger.info("Resume action %s names an execution that is gone; dropping it.", action.pk)
        return
    token = payload.get("token")
    resume_execution(
        execution,
        handle=str(payload.get("handle") or default_handle),
        token=str(token) if token else None,
    )


def _scoped(model: Any, workspace_id: Any, raw_id: Any) -> Any:
    """Fetch one row of ``model`` by id, inside ``workspace_id``, or ``None``.

    Scoped rather than ``.get(pk=...)``: the ids live in a JSON payload, and a
    lookup that crosses tenants because a payload said so is the same hole
    ``get_scoped_object_or_404`` exists to close on the request side.
    """
    if not raw_id or workspace_id is None:
        return None
    try:
        pk = UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return model.objects.for_workspace(workspace_id).filter(pk=pk).first()


def _connection(workspace_id: Any, raw_id: Any) -> Any:
    from apps.channels.models import ChannelConnection

    return _scoped(ChannelConnection, workspace_id, raw_id)
