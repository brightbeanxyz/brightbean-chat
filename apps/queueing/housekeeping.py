"""The hourly housekeeping chain and the registry other layers hang jobs off.

SPEC §15 lists what has to happen on a schedule: prune the webhook event log,
expire stale waiting executions, poll WhatsApp template statuses, reset zombies.
Only the last of those is this issue's work — every other item belongs to a
feature that has not landed yet. So what ships here is the *mechanism*: a
registry, a self-rescheduling action to drive it, and one job.

**How a later layer adds a job.** Same shape as the handler registry —
registration is an import side effect of your own app's ``ready()``::

    # apps/channels/housekeeping.py
    from apps.queueing.housekeeping import register_housekeeping_job

    @register_housekeeping_job("prune_webhook_event_log")
    def prune_webhook_event_log() -> str | None:
        ...
        return f"pruned {count} rows"       # returned text is logged

A job takes no arguments, runs across every tenant (that is what housekeeping
*is*), and must be idempotent: the sweep retries as a whole if any job fails,
so a job that already succeeded will run again.

**Why the chain cannot break.** The handler schedules its successor *before* it
runs anything. A sweep that throws still leaves next hour's row in the table, so
one bad hour cannot silently end all future housekeeping — which, since zombie
recovery is itself a housekeeping job, would otherwise be an outage that hides
itself. Per-hour idempotency keys mean two workers, a tick and a restart all
converge on the same single row.

The chain rides on a NULL-workspace ``ScheduledAction`` (see
``apps.queueing.models``): housekeeping belongs to the deployment, not to a
tenant, and a NULL workspace is invisible to every ``.for_workspace()`` query.
"""

import importlib
import importlib.util
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.queueing.locks import advisory_lock
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from apps.queueing.registry import register_handler, schedule_system

__all__ = [
    "HOUSEKEEPING_INTERVAL",
    "ZOMBIE_AFTER",
    "ensure_housekeeping_scheduled",
    "housekeeping_jobs",
    "register_housekeeping_job",
    "reset_zombie_actions",
    "run_housekeeping_jobs",
]

logger = logging.getLogger(__name__)

#: SPEC §15: housekeeping runs hourly.
HOUSEKEEPING_INTERVAL = timedelta(hours=1)

#: SPEC §15: "resets rows in status running with updated_at older than 10 min".
#:
#: The number is a bet that no handler legitimately runs for ten minutes. If one
#: ever does, it must checkpoint by touching ``updated_at`` — otherwise the
#: sweep hands its row to a second worker while the first is still working.
ZOMBIE_AFTER = timedelta(minutes=10)

#: Serialises the bootstrap check-then-create across every worker and tick.
#: Same mechanism as the contact locks (SPEC §9.6), different namespace.
BOOTSTRAP_LOCK_KEY = "queueing:housekeeping-bootstrap"

#: A job returns an optional summary string, which is logged.
HousekeepingJob = Callable[[], str | None]

_JOBS: dict[str, HousekeepingJob] = {}

#: Dotted paths already tried this process, resolved or not. See
#: :func:`_resolve_optional_jobs`.
_RESOLUTION_ATTEMPTED: set[str] = set()

#: Jobs owned by apps that may not be installed yet, wired by dotted path.
#:
#: The registry above is the real mechanism and the one every later issue should
#: use. This is the bridge for the three call sites SPEC §15 names by name: a
#: sibling can ship the function before it wires up ``ready()``, and a
#: deployment that does not install that app simply never resolves the path.
#: Resolution is lazy, failures are ignored, and each lands under its registry
#: name — so an app that also registers properly cannot cause a double run.
OPTIONAL_JOB_PATHS: tuple[tuple[str, str], ...] = (
    # issue #4 (L2-B): webhook_event_log rows older than 30 days.
    ("prune_webhook_event_log", "apps.channels.ingest.prune_webhook_event_log"),
    # issue #9 (L3-B): executions waiting longer than 30 days -> expired.
    ("expire_stale_executions", "apps.flows.housekeeping.expire_stale_executions"),
    # issue #19 (L5-C): poll Meta for template review outcomes.
    ("poll_whatsapp_templates", "apps.channels.providers.whatsapp.poll_template_statuses"),
)


def register_housekeeping_job(name: str, *, replace: bool = False) -> Callable[[HousekeepingJob], HousekeepingJob]:
    """Decorator adding a callable to the hourly sweep."""

    def decorator(func: HousekeepingJob) -> HousekeepingJob:
        existing = _JOBS.get(name)
        if existing is not None and not replace and existing is not func:
            raise RuntimeError(
                f"Housekeeping job {name!r} is already registered by "
                f"{existing.__module__}.{getattr(existing, '__qualname__', existing)}."
            )
        _JOBS[name] = func
        return func

    return decorator


def _resolve_optional_jobs() -> None:
    """Register the dotted-path jobs whose modules happen to be importable.

    Attempted once per process per path. Python does not cache *failed*
    imports, so without ``_RESOLUTION_ATTEMPTED`` every sweep would repeat a
    full ``sys.path`` search for each app that has not landed.
    """
    for name, path in OPTIONAL_JOB_PATHS:
        if name in _JOBS or path in _RESOLUTION_ATTEMPTED:
            continue
        module_path, _, attribute = path.rpartition(".")

        # "Not landed yet" and "installed but broken" both surface as
        # ImportError from import_module — the second one when a module the
        # target itself imports is missing. Treating them alike would file a
        # broken installation under "expected", at debug level, and memoise it
        # so the job stays silently off for the life of the process. find_spec
        # answers the question that actually distinguishes them: is the module
        # *there*.
        try:
            spec = importlib.util.find_spec(module_path)
        except (ImportError, ValueError):
            # A missing parent package also means absent.
            spec = None
        if spec is None:
            _RESOLUTION_ATTEMPTED.add(path)
            logger.debug("Optional housekeeping job %s not available yet (%s)", name, path)
            continue

        try:
            module = importlib.import_module(module_path)
        except Exception:  # noqa: BLE001 - see below; this must not reach the caller
            # The module is there and blew up on import — a misconfiguration, a
            # bad setting, a circular import, or one of its own imports missing.
            # A real fault somebody has to see, and one that must not propagate:
            # this runs outside run_housekeeping_jobs' per-job guard, so letting
            # it escape would abort the whole sweep and take zombie recovery
            # with it. Zombie recovery is what makes the queue self-healing.
            _RESOLUTION_ATTEMPTED.add(path)
            logger.exception("Optional housekeeping job %s could not be imported from %s", name, path)
            continue

        job = getattr(module, attribute, None)
        _RESOLUTION_ATTEMPTED.add(path)
        if callable(job):
            _JOBS[name] = job
            logger.info("Registered optional housekeeping job %s from %s", name, path)
        else:
            # The module is here and the attribute is not. Silence would be
            # indistinguishable from "the app has not landed", which is exactly
            # how a typo'd path becomes a job that never runs and nobody misses.
            logger.warning(
                "Optional housekeeping job %s: %s imported but exposes no callable %r; it will not run",
                name,
                module_path,
                attribute,
            )


def housekeeping_jobs() -> dict[str, HousekeepingJob]:
    """Every job that will run in the next sweep, resolved."""
    _resolve_optional_jobs()
    return dict(_JOBS)


def reset_zombie_actions(now: datetime | None = None) -> str:
    """Return abandoned ``running`` rows to ``pending`` (SPEC §15).

    A worker killed mid-batch — an OOM kill, a container eviction, a
    ``SIGKILL`` — leaves rows marked ``running`` that no process is running.
    Nothing else will ever pick them up: the claim only looks at ``pending``.
    This is the only thing that makes the queue self-healing rather than
    slowly leaking work.

    ``attempts`` was already incremented by the claim, so a repeatedly-crashing
    action still runs out of budget instead of looping forever.
    """
    cutoff = (now or timezone.now()) - ZOMBIE_AFTER
    # Cross-tenant on purpose: abandoned work belongs to whichever workspace
    # owned it, and housekeeping sweeps the whole deployment.
    abandoned = ScheduledAction.objects.unscoped().filter(status=ActionStatus.RUNNING, updated_at__lt=cutoff)

    # Budget first. An action that kills its worker — a segfault, an OOM kill —
    # never reaches _record_failure, so the only thing that ever counts its
    # attempts is the claim. Resetting it unconditionally means claim, die,
    # reset, claim, die, forever: max_attempts stops applying to exactly the
    # failure mode that most deserves it, and one poisonous row takes down a
    # worker every ten minutes indefinitely.
    exhausted = abandoned.filter(attempts__gte=F("max_attempts")).update(
        status=ActionStatus.FAILED,
        last_error=(
            "Abandoned while running and out of attempts: the worker holding it died "
            "without recording a failure. See docs/SPEC.md §15 (zombie recovery)."
        ),
        updated_at=timezone.now(),
    )
    reset = abandoned.filter(attempts__lt=F("max_attempts")).update(
        status=ActionStatus.PENDING, updated_at=timezone.now()
    )

    if reset:
        logger.warning("Zombie recovery returned %s abandoned action(s) to pending", reset)
    if exhausted:
        logger.error("Zombie recovery failed %s abandoned action(s) that were out of attempts", exhausted)
    return f"reset {reset} zombie action(s), failed {exhausted} out of attempts"


# Registered by call rather than as a decorator: the decorator narrows the name
# to the no-argument HousekeepingJob type, and the optional ``now`` above is what
# lets the zombie test drive the clock instead of waiting ten real minutes.
register_housekeeping_job("reset_zombie_actions")(reset_zombie_actions)


def run_housekeeping_jobs() -> list[str]:
    """Run every registered job. Returns the names that failed.

    Each job is isolated: one raising must not stop the others, or the first
    flaky job would starve every job registered after it.
    """
    failures: list[str] = []
    for name, job in housekeeping_jobs().items():
        try:
            summary = job()
        except Exception:  # noqa: BLE001 - one bad job must not end the sweep
            logger.exception("Housekeeping job %s failed", name)
            failures.append(name)
        else:
            logger.info("Housekeeping job %s: %s", name, summary or "ok")
    return failures


def _bucket(moment: datetime) -> str:
    """The hour a housekeeping run belongs to, as an idempotency key suffix."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H")


def _schedule_run(run_at: datetime) -> ScheduledAction:
    # schedule_system, not schedule: this row belongs to the deployment and
    # carries a NULL workspace on purpose. The separate entry point is what
    # keeps that deliberate rather than an argument someone could pass by
    # accident (apps.queueing.registry).
    return schedule_system(
        ActionType.HOUSEKEEPING,
        run_at,
        idempotency_key=f"housekeeping:{_bucket(run_at)}",
    )


@register_handler(ActionType.HOUSEKEEPING)
def handle_housekeeping(payload: dict[str, Any], action: ScheduledAction) -> None:
    """Run the sweep, having first guaranteed there will be a next one.

    **This must not raise on a failed job**, and the reason is the transaction
    it runs inside. ``process_action`` wraps the handler in ``atomic()``, so an
    exception here rolls back everything the sweep just did — the successor row
    scheduled on the first line included. Raising to "retry with backoff" would
    therefore destroy the very chain that line exists to guarantee, along with
    every job that had already succeeded, zombie recovery among them. One
    permanently broken job would end all housekeeping.

    So failures are logged and the sweep completes. That is also the right
    semantics for a periodic job: the retry for an hourly sweep is the next
    hourly sweep, not a backoff ladder, and every job is required to be
    idempotent precisely so re-running it costs nothing.
    """
    # Before the jobs, deliberately: the successor is committed with the rest of
    # this transaction, so the chain survives anything the jobs do.
    following = _schedule_run(timezone.now() + HOUSEKEEPING_INTERVAL)
    logger.debug("Next housekeeping sweep scheduled for %s (%s)", following.run_at, following.pk)

    failures = run_housekeeping_jobs()
    if failures:
        logger.error(
            "Housekeeping jobs failed and will be retried by the next hourly sweep: %s",
            ", ".join(failures),
        )


def ensure_housekeeping_scheduled() -> ScheduledAction:
    """Guarantee a pending housekeeping action exists. Idempotent.

    Called at the start of every worker, every ``tick`` and every
    ``/internal/tick`` request, which is what makes the chain self-healing: a
    deployment that lost its row to a manual delete, an exhausted retry budget
    or a database restore gets it back on the next worker start, and a
    cron-only host that never runs ``process_tasks`` still gets housekeeping.

    Bootstrapping here rather than in a data migration is that same property:
    a migration runs once and cannot repair anything afterwards.
    """
    # Check-then-create is a race between two workers booting together, and an
    # hour-bucket idempotency key cannot settle it: the row this repairs is
    # often a *failed* row from the current hour, so the key it would collide
    # with is the dead row itself and the chain would stay dead. An advisory
    # lock makes the check and the create one step instead.
    with transaction.atomic(), advisory_lock(BOOTSTRAP_LOCK_KEY):
        return _live_or_new_chain()


def _live_or_new_chain() -> ScheduledAction:
    """The bootstrap critical section. The caller holds the advisory lock."""
    # Cross-tenant on purpose, and the only read that can see a system row at
    # all: NULL workspace means no .for_workspace() query matches it.
    #
    # workspace__isnull=True is load bearing, not decoration. Without it any
    # row that merely *has type* "housekeeping" satisfies the liveness check —
    # including a tenant-owned one from a fixture, a test, or a later layer
    # reusing the name. The system chain would then never be created, and every
    # worker, tick and HTTP tick would report it healthy while zombie recovery
    # and every registered prune silently never ran.
    #
    # A RUNNING row only counts as live while it is *fresh*. If the worker
    # holding the sweep dies, the row stays running forever and nothing else
    # can move it: the claim only looks at pending rows, and the one thing that
    # resets abandoned rows is zombie recovery — which is a housekeeping job,
    # inside the sweep that is now stuck. Accepting a stale running row here
    # closed that loop and stopped all housekeeping permanently, with every
    # worker start reporting the chain healthy. Past ZOMBIE_AFTER it no longer
    # suppresses a fresh chain, and the new chain's first sweep reclaims it.
    fresh_enough = timezone.now() - ZOMBIE_AFTER
    live = (
        ScheduledAction.objects.unscoped()
        .filter(type=ActionType.HOUSEKEEPING, workspace__isnull=True)
        .filter(Q(status=ActionStatus.PENDING) | Q(status=ActionStatus.RUNNING, updated_at__gte=fresh_enough))
        .order_by("run_at")
        .first()
    )
    if live is not None:
        return live

    # Deliberately no idempotency key: the lock above already guarantees
    # uniqueness, and a key would be the thing preventing the repair.
    #
    # Now, not next hour — a deployment booting for the first time, or
    # recovering from a broken chain, should sweep promptly rather than sit
    # idle for an hour with zombies in the table.
    return schedule_system(ActionType.HOUSEKEEPING, timezone.now())
