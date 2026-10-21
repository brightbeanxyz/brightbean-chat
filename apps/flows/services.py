"""Flow lifecycle: draft saves, publishing, and the small edits the list page makes.

**Every write locks the flow row first.** ``SELECT … FOR UPDATE`` on the parent
``Flow`` is what makes version numbers safe: two builders autosaving the same
flow at the same moment would otherwise both read "latest is 3" and both try to
create version 4, and the unique constraint would turn one of them into a 500
rather than a save. Serialising on the flow makes the read-then-write atomic,
and the constraint stays as the assertion behind it rather than the mechanism.

The same lock makes publishing atomic: clearing the old published flag, setting
the new one and stamping ``flow.status`` happen in one transaction, so no reader
can see a flow with two published versions or none.

**Which lock.** Saves and publishes take ``FOR NO KEY UPDATE``: it serialises
them against each other exactly as ``FOR UPDATE`` would, but not against the
``FOR KEY SHARE`` every :func:`apps.flows.engine.start_flow` holds on the flow
until its execution is committed, so a busy flow's conversations never hold up
an autosave. :func:`take_offline` alone takes the full ``FOR UPDATE``, because
waiting for the starts in flight is the point there.

Nothing here reaches across tenants. Every query goes through
``for_workspace(flow.workspace_id)``, so there is no ``.unscoped()`` in this app
at all — the caller has already resolved the flow through
``get_scoped_object_or_404``, and re-scoping costs an indexed column in a WHERE
clause that was going to filter on the primary key anyway.
"""

from dataclasses import dataclass, replace
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from apps.flows.capabilities import connected_platforms
from apps.flows.models import Flow, FlowStatus, FlowVersion, Trigger
from apps.flows.schema import ValidationResult, empty_graph, validate_graph
from apps.flows.schema.sanitize import sanitize_graph

__all__ = [
    "FlowNotLiveError",
    "FlowValidationError",
    "PublishResult",
    "archive_flow",
    "create_flow",
    "duplicate_flow",
    "latest_version",
    "locked_status",
    "publish",
    "published_version",
    "rename_flow",
    "restore_flow",
    "save_draft",
    "set_folder",
    "take_offline",
    "validate_for_workspace",
]


class FlowPlanLimitError(ValueError):
    """The organization's plan has no room for another active automation.

    A ``ValueError``, like every other refusal a caller of this module is
    written to catch — not a ``FlowValidationError``, which carries a
    ``ValidationResult`` and would make a billing limit look like a broken graph.
    """


class FlowNotLiveError(ValueError):
    """This flow cannot be taken offline: it is not live, or it is a broadcast's own."""


class FlowValidationError(Exception):
    """Publishing was refused. ``result`` carries the findings to show the author."""

    def __init__(self, result: ValidationResult) -> None:
        super().__init__("The flow has validation errors and cannot be published.")
        self.result = result


@dataclass(frozen=True)
class PublishResult:
    """What :func:`publish` did, and what validation said while doing it.

    The findings come back with the version because the caller needs them for
    its response and ``publish`` has just computed them. Returning only the
    version made every caller re-validate the same graph a second time, outside
    the transaction that had just approved it.
    """

    version: "FlowVersion"
    validation: ValidationResult


def validate_for_workspace(
    graph: Any,
    workspace: Any,
    *,
    known_size: int | None = None,
    flow: Any = None,
) -> ValidationResult:
    """Validate a graph with the platforms it will actually run on in view.

    ``flow`` narrows the platform set to what that flow is *triggered* on
    (issue #11): a flow whose only trigger is an SMS keyword should warn about
    its buttons, and one triggered only on Telegram should not be warned about
    what SMS cannot do. A flow with no platform-bearing trigger — none at all, or
    only ``api`` and ``rule`` ones — falls back to the workspace's connected
    platforms, which is the answer this function gave before triggers existed.

    The argument is optional so a caller holding only a workspace keeps that
    behaviour without changing.

    ``known_size`` lets a caller that already knows an upper bound on the
    serialized size skip re-measuring it (:func:`apps.flows.schema.envelope.check_limits`).
    """
    if flow is not None:
        from apps.flows.triggers.platforms import platforms_for_flow

        platforms = platforms_for_flow(flow)
    else:
        platforms = connected_platforms(workspace)
    result = validate_graph(graph, platforms=platforms, known_size=known_size)
    if flow is None:
        return result

    # "Is this graph well formed?" and "will anything ever reach it?" are
    # different questions, and only the first was ever asked. A template
    # imported into a workspace with no matching channel published clean, showed
    # Live, and could not run. See apps/flows/triggers/readiness.py.
    from apps.flows.triggers.readiness import readiness_warnings

    ready = readiness_warnings(flow, connected=set(connected_platforms(workspace)))
    if not ready:
        return result
    # A new result rather than a mutation: ValidationResult is frozen, and these
    # go last so a capability finding about the graph still reads first.
    return replace(result, warnings=[*result.warnings, *ready])


def _versions(flow: Flow) -> Any:
    return FlowVersion.objects.for_workspace(flow.workspace_id).filter(flow=flow)


def latest_version(flow: Flow) -> FlowVersion | None:
    """The newest version — the draft the builder edits, unless it is published."""
    return _versions(flow).order_by("-version").first()


def published_version(flow: Flow) -> FlowVersion | None:
    return _versions(flow).filter(published=True).first()


@transaction.atomic
def create_flow(*, workspace: Any, name: str, folder: str = "", user: Any = None, graph: Any = None) -> Flow:
    """A new flow, with version 1 already there as a draft.

    Creating the first version here rather than lazily means every read path can
    assume a draft exists, and the builder never has to special-case a flow with
    nothing in it.

    ``graph`` defaults to empty on purpose. The importer and the broadcast
    composer both call this and then overwrite version 1 immediately, and
    several tests read "still empty" as "nothing was written" — so seeding every
    caller would turn those into false greens rather than failures. Only the
    Create button on the flows list asks for a starter; the product decision
    stays at the product surface.
    """
    flow = Flow(workspace=workspace, name=name, folder=folder, status=FlowStatus.DRAFT)
    flow.save()
    FlowVersion(
        workspace=flow.workspace,
        flow=flow,
        version=1,
        graph_json=empty_graph() if graph is None else graph,
        created_by=user,
    ).save()
    return flow


def rename_flow(flow: Flow, name: str) -> Flow:
    flow.name = name
    flow.save(update_fields=["name", "updated_at"])
    return flow


def set_folder(flow: Flow, folder: str) -> Flow:
    flow.folder = folder
    flow.save(update_fields=["folder", "updated_at"])
    return flow


def archive_flow(flow: Flow) -> Flow:
    """Archive a flow. Its published version stays put so history reads back."""
    flow.status = FlowStatus.ARCHIVED
    flow.save(update_fields=["status", "updated_at"])
    return flow


#: The two share-mode row locks Django's ``select_for_update`` cannot spell.
#: Literal statements rather than one formatted string, so the SQL is never
#: assembled at run time.
_STATUS_UNDER_LOCK = {
    "key share": "SELECT status FROM flows_flow WHERE id = %s FOR KEY SHARE",
    "share": "SELECT status FROM flows_flow WHERE id = %s FOR SHARE",
}


def locked_status(flow: Flow, *, lock: str) -> str | None:
    """Re-read ``flow``'s status under a share-mode row lock; ``None`` if it is gone.

    Must run inside a transaction: the lock is held until it ends, which is the
    whole point. ``"key share"`` is what :func:`apps.flows.engine.start_flow`
    holds while it creates an execution — the weakest lock, compatible with
    every other start and with the ``FOR NO KEY UPDATE`` that saves and
    publishes take. ``"share"`` is :func:`take_offline`'s second step: it lets
    starts through but makes a publish wait, so the flow cannot be set live
    again while its conversations are being stopped. Only ``FOR UPDATE`` —
    ``take_offline``'s first step — waits for the starts.
    """
    with connection.cursor() as cursor:
        cursor.execute(_STATUS_UNDER_LOCK[lock], [flow.pk])
        row = cursor.fetchone()
    return row[0] if row else None


def take_offline(flow: Flow) -> int:
    """Take a live flow offline. Returns how many conversations in it were stopped.

    The builder's "Set offline", in two steps.

    **First, under** ``FOR UPDATE`` **on the flow row**: the published flag
    goes and the status becomes ``offline``. That one write is what stops the flow
    everywhere — trigger matching and rule triggers want an ``active`` flow, and
    every other way in (the public API, a sequence step, another flow's
    ``start_flow``, a start from a contact's page, a queued start) goes through
    :func:`apps.flows.engine.start_flow`, which re-reads the flow under a share
    lock before resolving its published version. A start already past that
    point holds the lock, so this waits for it to commit, and step two then
    sees the execution it made. Setting it live again is an ordinary
    :func:`publish`, plan check included, since offline flows do not count
    towards the plan's automation limit. The version that was live keeps its
    ``published_at`` and so stays frozen (see :func:`save_draft`).

    **Then, under** ``FOR SHARE``, conversations already in the flow stop,
    through :func:`apps.flows.engine.expire_flow_executions`. Not inside the
    first transaction: that waits on execution rows a running step holds, and a
    step whose ``start_flow`` node restarts this same flow is waiting on the
    flow row while holding its execution — ``FOR UPDATE`` held across both
    would deadlock. ``FOR SHARE`` does not block that step's share lock, but it
    does block a publish, so nobody can set the flow live again mid-way. If
    somebody already did, in the moment between the two steps, nothing is
    stopped: the conversations running now belong to a flow that is live again.

    Triggers are left as they are: pausing them would lose which ones somebody
    had paused on purpose, and an offline flow is not matched whatever they say.
    A broadcast's private flow is refused: its sends name their version
    explicitly, so it would keep starting conversations, and the broadcast has
    its own cancel.
    """
    # Late: the engine imports this module for `published_version`.
    from apps.flows.engine import expire_flow_executions

    with transaction.atomic():
        locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update().get(pk=flow.pk)
        if locked.status != FlowStatus.ACTIVE:
            raise FlowNotLiveError("Only a live flow can be set offline.")
        if Flow.objects.for_workspace(flow.workspace_id).filter(pk=locked.pk, broadcasts__isnull=False).exists():
            raise FlowNotLiveError("This flow belongs to a broadcast. Cancel the broadcast to stop it.")

        _versions(locked).filter(published=True).update(published=False)
        locked.status = FlowStatus.OFFLINE
        locked.save(update_fields=["status", "updated_at"])

    # The caller holds the unlocked instance, as in publish().
    flow.status = locked.status

    with transaction.atomic():
        if locked_status(locked, lock="share") == FlowStatus.ACTIVE:
            return 0
        return expire_flow_executions(locked)


def restore_flow(flow: Flow) -> Flow:
    """Un-archive. Back to active if something is published, draft otherwise.

    Un-archiving a flow that has a published version puts it back to ACTIVE,
    which is the same lever :func:`publish` pulls — so it takes the same plan
    check. Without it, an organization over its automation limit could archive
    and restore its way past the cap one flow at a time.
    """
    if published_version(flow):
        _check_plan_allows_activation(flow)
    flow.status = FlowStatus.ACTIVE if published_version(flow) else FlowStatus.DRAFT
    flow.save(update_fields=["status", "updated_at"])
    return flow


@transaction.atomic
def duplicate_flow(flow: Flow, *, user: Any = None) -> Flow:
    """Copy a flow's newest graph into a fresh draft flow.

    The copy is never published, whatever the original was: publishing is an act,
    and inheriting it would put an unreviewed flow live under a new name.
    """
    source = latest_version(flow)
    # Trim the name, not the composed string: slicing after the concatenation
    # drops the suffix entirely for a name at the field limit, and the copy then
    # has a name identical to its original.
    suffix = " (copy)"
    limit = Flow._meta.get_field("name").max_length or 200
    copy = Flow(
        workspace=flow.workspace,
        name=f"{flow.name[: limit - len(suffix)]}{suffix}",
        folder=flow.folder,
        status=FlowStatus.DRAFT,
    )
    copy.save()
    FlowVersion(
        workspace=copy.workspace,
        flow=copy,
        version=1,
        graph_json=source.graph_json if source else empty_graph(),
        created_by=user,
    ).save()
    return copy


@transaction.atomic
def save_draft(flow: Flow, graph_json: Any, *, user: Any = None) -> FlowVersion:
    """Write the graph to the latest draft, opening a new version if needed.

    The lock is the point: without it, two concurrent saves both read the same
    "latest" and race to allocate the same version number.

    **Markup is normalised here**, before the document is stored, because this
    is the one write path every client shares. A node config field declared as
    HTML (``NodeSpec.html_fields`` — today only ``send_email``'s ``html_body``)
    goes through the same allowlist the email adapter applies at send time, so
    what is stored can never be markup that the builder's body editor would
    then write into another member's browser. Doing it in the editor alone
    would leave the API's ``PUT`` open, and ``edit_flows`` is enough to call it.
    """
    graph_json = sanitize_graph(graph_json)
    locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update(no_key=True).get(pk=flow.pk)
    latest = _versions(locked).order_by("-version").first()

    # `published_at` as well as `published`: a version taken offline is no
    # longer published, but conversations ran on it, so it is history rather
    # than a draft and an edit opens the next version like any edit of a live
    # flow.
    if latest is not None and not latest.published and latest.published_at is None:
        # `created_by` is not touched. It records who opened this revision, and
        # SPEC §5 gives the column no other meaning; rewriting it on every
        # autosave would make it name whoever last had the flow open, which
        # during ordinary co-editing is not the author of anything.
        latest.graph_json = graph_json
        latest.save(update_fields=["graph_json", "updated_at"])
        return latest

    draft = FlowVersion(
        workspace=locked.workspace,
        flow=locked,
        version=(latest.version + 1) if latest else 1,
        graph_json=graph_json,
        created_by=user,
    )
    draft.save()
    return draft


@transaction.atomic
def publish(flow: Flow, *, user: Any = None) -> PublishResult:
    """Validate strictly and publish the newest version. Raises on any error.

    Warnings do not stop a publish — SPEC §9.1 is explicit that capability
    findings are non-blocking, and a flow that mentions a channel the workspace
    has not connected yet is a normal state on the way to connecting it.
    """
    locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update(no_key=True).get(pk=flow.pk)
    target = _versions(locked).order_by("-version").first()
    if target is None:  # pragma: no cover - create_flow always makes version 1
        raise FlowValidationError(validate_graph(empty_graph()))

    result = validate_for_workspace(target.graph_json, locked.workspace, flow=locked)
    if not result.is_publishable:
        raise FlowValidationError(result)

    # The organization's plan. Checked while holding the row lock taken above,
    # which is what apps/media_library/quotas.py requires of any read-then-write
    # count: without it two concurrent publishes both read the old total and
    # both pass. Only a flow that is not already active spends a slot, so
    # re-publishing a live flow is always allowed.
    if locked.status != FlowStatus.ACTIVE:
        _check_plan_allows_activation(locked)

    if not target.published:
        _versions(locked).filter(published=True).update(published=False)
        target.published = True
        # The first time only: a version set live again after going offline
        # keeps the moment it first ran.
        target.published_at = target.published_at or timezone.now()
        target.save(update_fields=["published", "published_at", "updated_at"])

    if locked.status != FlowStatus.ACTIVE:
        locked.status = FlowStatus.ACTIVE
        locked.save(update_fields=["status", "updated_at"])

    # Publishing an entirely paused flow now starts its configured triggers.
    # A mixed set is deliberate: keep the individually paused triggers paused.
    # Hold the trigger rows until commit so a concurrent publish cannot make a
    # second decision from the same all-off snapshot.
    triggers = list(Trigger.objects.for_workspace(flow.workspace_id).filter(flow=locked).select_for_update())
    if triggers and not any(trigger.enabled for trigger in triggers):
        Trigger.objects.for_workspace(flow.workspace_id).filter(pk__in=[trigger.pk for trigger in triggers]).update(
            enabled=True, updated_at=timezone.now()
        )
        # Readiness and capability warnings depend on enabled triggers. Return
        # the verdict for the committed state, not the earlier all-off state.
        result = validate_for_workspace(target.graph_json, locked.workspace, flow=locked)

    # The caller holds the unlocked instance; keep it honest rather than making
    # every call site remember to refresh.
    flow.status = locked.status
    return PublishResult(version=target, validation=result)


def _check_plan_allows_activation(flow: Flow) -> None:
    """Refuse activating a flow the organization's plan has no room for.

    Re-raised as ``FlowValidationError``'s sibling rather than letting
    ``PlanLimitError`` out: every caller of :func:`publish` already handles this
    module's own errors, and a new exception type escaping into those views
    would be a 500 rather than a message.

    A late import — billing reads this app's models, so a module-scope import
    would close the loop, and an unconfigured deployment should never load it.
    """
    from apps.billing.entitlements import (
        PlanLimitError,
        check_can_activate_automation,
        organization_locked,
    )

    organization = flow.workspace.organization
    try:
        # Locked, not merely counted: `publish` holds its own flow row, which
        # says nothing about the *other* flows the count walks. Two publishes at
        # once would otherwise both read `limit - 1`.
        with organization_locked(organization):
            check_can_activate_automation(organization)
    except PlanLimitError as exc:
        raise FlowPlanLimitError(str(exc)) from exc
