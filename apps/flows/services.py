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

Nothing here reaches across tenants. Every query goes through
``for_workspace(flow.workspace_id)``, so there is no ``.unscoped()`` in this app
at all — the caller has already resolved the flow through
``get_scoped_object_or_404``, and re-scoping costs an indexed column in a WHERE
clause that was going to filter on the primary key anyway.
"""

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from apps.flows.capabilities import connected_platforms
from apps.flows.models import Flow, FlowStatus, FlowVersion
from apps.flows.schema import ValidationResult, empty_graph, validate_graph

__all__ = [
    "FlowValidationError",
    "PublishResult",
    "archive_flow",
    "create_flow",
    "duplicate_flow",
    "latest_version",
    "publish",
    "published_version",
    "rename_flow",
    "restore_flow",
    "save_draft",
    "set_folder",
    "validate_for_workspace",
]


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
    return validate_graph(graph, platforms=platforms, known_size=known_size)


def _versions(flow: Flow) -> Any:
    return FlowVersion.objects.for_workspace(flow.workspace_id).filter(flow=flow)


def latest_version(flow: Flow) -> FlowVersion | None:
    """The newest version — the draft the builder edits, unless it is published."""
    return _versions(flow).order_by("-version").first()


def published_version(flow: Flow) -> FlowVersion | None:
    return _versions(flow).filter(published=True).first()


@transaction.atomic
def create_flow(*, workspace: Any, name: str, folder: str = "", user: Any = None) -> Flow:
    """A new flow, with version 1 already there as an empty draft.

    Creating the first version here rather than lazily means every read path can
    assume a draft exists, and the builder never has to special-case a flow with
    nothing in it.
    """
    flow = Flow(workspace=workspace, name=name, folder=folder, status=FlowStatus.DRAFT)
    flow.save()
    FlowVersion(workspace=flow.workspace, flow=flow, version=1, graph_json=empty_graph(), created_by=user).save()
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


def restore_flow(flow: Flow) -> Flow:
    """Un-archive. Back to active if something is published, draft otherwise."""
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
    """
    locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update().get(pk=flow.pk)
    latest = _versions(locked).order_by("-version").first()

    if latest is not None and not latest.published:
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
    locked = Flow.objects.for_workspace(flow.workspace_id).select_for_update().get(pk=flow.pk)
    target = _versions(locked).order_by("-version").first()
    if target is None:  # pragma: no cover - create_flow always makes version 1
        raise FlowValidationError(validate_graph(empty_graph()))

    result = validate_for_workspace(target.graph_json, locked.workspace, flow=locked)
    if not result.is_publishable:
        raise FlowValidationError(result)

    if not target.published:
        _versions(locked).filter(published=True).update(published=False)
        target.published = True
        target.save(update_fields=["published", "updated_at"])

    if locked.status != FlowStatus.ACTIVE:
        locked.status = FlowStatus.ACTIVE
        locked.save(update_fields=["status", "updated_at"])

    # The caller holds the unlocked instance; keep it honest rather than making
    # every call site remember to refresh.
    flow.status = locked.status
    return PublishResult(version=target, validation=result)
