"""Two sweeps: executions nobody will answer, and uploads nobody confirmed.

    Housekeeping (hourly, via self-rescheduling action): […] expire stale
    waiting executions (waiting > 30 d -> expired) […]

Thirty days of waiting means the question was never answered, and the row is
doing active harm by then rather than merely sitting there: SPEC §22 allows one
live execution per contact, so a forgotten wait from last month is what stops
this month's keyword from starting anything.

``apps/queueing/housekeeping.py`` already names this exact dotted path in
``OPTIONAL_JOB_PATHS`` — it was written with this issue in mind — so the job
would be found even without the decorator below. It is registered explicitly
anyway: the lazy path resolver skips a name already in the registry, so there is
no double run, and an explicit registration is the one that survives somebody
tidying that tuple.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from apps.flows.models import ExecutionStatus, FlowExecution, FlowImport, FlowImportStatus
from apps.queueing.housekeeping import register_housekeeping_job

__all__ = ["IMPORTS_KEPT_FOR", "STALE_AFTER", "discard_stale_imports", "expire_stale_executions"]

logger = logging.getLogger(__name__)

#: SPEC §15: "waiting > 30 d -> expired".
STALE_AFTER = timedelta(days=30)

#: How long an unconfirmed flow-template upload is kept (issue #27).
#:
#: A pending ``FlowImport`` holds somebody's whole automation in a jsonb column
#: and has created nothing, so an abandoned one is pure storage. A week is long
#: enough that "I will finish the mapping tomorrow" survives a weekend and short
#: enough that a workspace does not accumulate strangers' templates.
IMPORTS_KEPT_FOR = timedelta(days=7)

#: Only the waiting statuses. A ``running`` execution older than the cutoff is a
#: different fault — a worker died mid-step — and it belongs to the queue's
#: zombie recovery, which will re-run the action rather than kill the run.
_STALE_STATUSES = (ExecutionStatus.WAITING_REPLY, ExecutionStatus.WAITING_DELAY)


@register_housekeeping_job("expire_stale_executions")
def expire_stale_executions() -> str:
    """Mark long-abandoned waits ``expired``. Idempotent, as every job must be."""
    cutoff = timezone.now() - STALE_AFTER
    # Cross-tenant on purpose: housekeeping sweeps the whole deployment, and a
    # stale execution belongs to whichever workspace owns it (CONTRIBUTING.md).
    stale = FlowExecution.objects.unscoped().filter(status__in=_STALE_STATUSES, updated_at__lt=cutoff)
    expired = stale.update(status=ExecutionStatus.EXPIRED, wait_config={}, updated_at=timezone.now())
    if expired:
        logger.info("Expired %s flow execution(s) that had been waiting since before %s", expired, cutoff)
    return f"expired {expired} stale execution(s)"


@register_housekeeping_job("discard_stale_flow_imports")
def discard_stale_imports() -> str:
    """Delete flow-template uploads nobody confirmed. Idempotent, as every job must be.

    Only ``pending`` rows, and they are **deleted** rather than marked
    ``discarded``: the point of the sweep is to stop holding the document, and a
    row whose ``document`` column has been emptied is a tombstone nothing reads.
    An ``applied`` row is kept — it is the record of where a workspace's flows
    came from.
    """
    cutoff = timezone.now() - IMPORTS_KEPT_FOR
    # Cross-tenant on purpose: housekeeping sweeps the whole deployment, and an
    # abandoned upload belongs to whichever workspace owns it (CONTRIBUTING.md).
    stale = FlowImport.objects.unscoped().filter(status=FlowImportStatus.PENDING, created_at__lt=cutoff)
    discarded, _ = stale.delete()
    if discarded:
        logger.info("Discarded %s unconfirmed flow import(s) from before %s", discarded, cutoff)
    return f"discarded {discarded} unconfirmed flow import(s)"
