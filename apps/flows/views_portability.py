"""Export a flow, and the three-step wizard that imports one (issue #27).

A module of its own rather than more of ``apps/flows/views.py``, following
``views_triggers.py``: these six views share one concern and none of the others
do.

**Both halves require ``edit_flows``.** Import obviously does — it creates
flows, tags, fields and sequences. Export does too, and the reason is worth
stating because it is not confidentiality: ``GET /w/<id>/api/flows/<id>/``
already serves the whole graph to any member, so gating the download lower
would protect nothing. It is gated because export is an *authoring* action on
the flow surface, ``edit_flows`` is the key this feature is written against
(``docs/agent-prompts/layer-7.md``), and one gate for the whole feature is
easier to reason about than two.

--------------------------------------------------------------------------
The wizard, and the promise it keeps
--------------------------------------------------------------------------

``upload`` → ``review`` → ``confirm``. The upload validates and stores; the
review asks the mapping questions and shows the dry run; only the confirm
writes. **Nothing but the ``FlowImport`` row exists before the confirm**, which
is the issue's "no object creation before dry-run confirm" and is asserted
directly in ``apps/flows/tests/test_portability_import.py``.

Everything an imported document contains is rendered through Django's
autoescaping — no ``|safe``, no ``mark_safe`` anywhere in the templates this
serves. A template's message bodies carry ``{{placeholders}}`` which
``apps.flows.rendering`` substitutes at send time and no template engine ever
evaluates (SECURITY-BASELINE §3); showing one on a review page must not be the
exception that undoes that.
"""

import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.campaigns.errors import CampaignsError
from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts.errors import ContactsError
from apps.flows import portability
from apps.flows.compat import installed_model
from apps.flows.models import Flow, FlowImport, FlowImportStatus
from apps.flows.picklists import picklists
from apps.flows.portability.envelope import MAX_DOCUMENT_BYTES
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

__all__ = [
    "flow_export",
    "flow_export_bundle",
    "import_confirm",
    "import_discard",
    "import_review",
    "import_start",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _download(request: WorkspaceRequest, flow_id: str, *, bundle: bool) -> HttpResponse:
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)
    document = portability.export_document(flow, bundle=bundle)
    response = HttpResponse(portability.serialize(document), content_type="application/json")
    # The filename is derived from the flow's name through an ASCII slug, so no
    # author text reaches this header unescaped.
    response["Content-Disposition"] = f'attachment; filename="{portability.export_filename(flow, bundle=bundle)}"'
    return response


@login_required
@require_permission("edit_flows")
@require_GET
def flow_export(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """This flow alone, as a downloadable template."""
    return _download(request, flow_id, bundle=False)


@login_required
@require_permission("edit_flows")
@require_GET
def flow_export_bundle(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """This flow and everything it hands over to, as one file."""
    return _download(request, flow_id, bundle=True)


# ---------------------------------------------------------------------------
# Import, step one: upload
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_http_methods(["GET", "POST"])
def import_start(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The upload page, and the upload itself.

    The size cap is applied to the bytes before they are parsed, and the parse
    before anything is stored — so an oversized or malformed file costs a length
    check and a failed ``json.loads``, never a database write.
    """
    if request.method == "GET":
        return render(
            request,
            "flows/import_upload.html",
            {
                "max_bytes": MAX_DOCUMENT_BYTES,
                "list_url": reverse("flows:list", kwargs={"workspace_id": workspace_id}),
            },
        )

    upload = request.FILES.get("file")
    if upload is None:
        return _upload_failed(request, workspace_id, ["Choose a flow template file to import."])
    # ``size`` is Optional on an UploadedFile, and a missing one is not a licence
    # to skip the cap: it falls through to zero here and the cap is applied again
    # to the bytes themselves in ``parse``, which is the one that cannot be lied to.
    size = upload.size or 0
    if size > MAX_DOCUMENT_BYTES:
        return _upload_failed(
            request,
            workspace_id,
            [f"That file is {size} bytes; the limit is {MAX_DOCUMENT_BYTES} bytes."],
        )

    document, issues = portability.parse_and_validate(upload.read())
    if document is None:
        return _upload_failed(request, workspace_id, [issue.message for issue in issues])

    record = FlowImport(
        workspace=request.workspace,
        document=document,
        mapping=portability.default_mapping(request.workspace, document, user=request.user),
        original_filename=str(upload.name or "")[:255],
        created_by=request.user,
    )
    record.save()
    return _redirect_to_review(workspace_id, record)


def _upload_failed(request: WorkspaceRequest, workspace_id: str, errors: list[str]) -> HttpResponse:
    """Re-render the upload page with what was wrong. Nothing was stored."""
    return render(
        request,
        "flows/import_upload.html",
        {
            "max_bytes": MAX_DOCUMENT_BYTES,
            "errors": errors[:20],
            "list_url": reverse("flows:list", kwargs={"workspace_id": workspace_id}),
        },
        status=400,
    )


def _redirect_to_review(workspace_id: str, record: FlowImport) -> HttpResponse:
    return redirect("flows:import_review", workspace_id=workspace_id, flow_import_id=record.pk)


# ---------------------------------------------------------------------------
# Import, step two: map and dry-run
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_http_methods(["GET", "POST"])
def import_review(request: WorkspaceRequest, workspace_id: str, flow_import_id: str) -> HttpResponse:
    """The mapping form and the dry run. A POST saves the answers and redirects.

    Saving on POST rather than only on confirm means a long mapping survives a
    reload, and it keeps the confirm a single, obvious act rather than a form
    submission that also happens to answer twenty questions. Post-redirect-get,
    so re-reading the dry run is a refresh rather than a re-submission.
    """
    record = get_scoped_object_or_404(FlowImport, request.workspace, pk=flow_import_id)

    if request.method == "POST":
        record.mapping = _mapping_from(request, record)
        record.save(update_fields=["mapping", "updated_at"])
        return _redirect_to_review(workspace_id, record)

    plan = portability.plan_import(request.workspace, record.document, record.mapping)
    return render(request, "flows/import_review.html", _review_context(request, workspace_id, record, plan))


def _review_context(
    request: WorkspaceRequest, workspace_id: str, record: FlowImport, plan: portability.ImportPlan
) -> dict[str, Any]:
    return {
        "record": record,
        "plan": plan,
        "applied": record.status == FlowImportStatus.APPLIED,
        # Grouped for rendering: one section per kind, in the manifest's order,
        # each question already carrying the options it may be answered with.
        # Computing them here rather than in the template is what keeps the
        # template free of per-kind branching over six different querysets.
        "groups": _groups(request.workspace, plan),
        "review_url": reverse(
            "flows:import_review", kwargs={"workspace_id": workspace_id, "flow_import_id": record.pk}
        ),
        "confirm_url": reverse(
            "flows:import_confirm", kwargs={"workspace_id": workspace_id, "flow_import_id": record.pk}
        ),
        "discard_url": reverse(
            "flows:import_discard", kwargs={"workspace_id": workspace_id, "flow_import_id": record.pk}
        ),
        "list_url": reverse("flows:list", kwargs={"workspace_id": workspace_id}),
    }


def _groups(workspace: Any, plan: portability.ImportPlan) -> list[dict[str, Any]]:
    """The resolutions, one section per kind, in the manifest's fixed order."""
    by_kind: dict[str, list[Any]] = {}
    for resolution in plan.resolutions:
        by_kind.setdefault(resolution.requirement.kind, []).append(resolution)

    lists = picklists(workspace)
    return [
        {
            "kind": kind,
            "label": _KIND_LABELS.get(kind, kind),
            "help": _KIND_HELP.get(kind, ""),
            "field_types": _field_types() if kind == "custom_field" else [],
            "questions": [
                {"resolution": resolution, "options": _options(workspace, lists, resolution.requirement)}
                for resolution in by_kind[kind]
            ],
        }
        for kind in portability.REQUIREMENT_KINDS
        if kind in by_kind
    ]


#: How many library assets the media picker offers. A workspace can hold far
#: more; a ``<select>`` of ten thousand is not a picker, and the URL box beside
#: it is the answer for anything not in the list.
MEDIA_OPTIONS = 200


def _options(workspace: Any, lists: dict[str, list[dict[str, Any]]], requirement: Any) -> list[dict[str, str]]:
    """The ``{id, label}`` choices one requirement may be answered with.

    Everything comes from ``picklists`` where ``picklists`` already has it, so
    the import wizard offers the same lists the builder's config panels do.
    Segments and media assets are the two the builder never needed, and both are
    read here through ``for_workspace`` like everything else.
    """
    kind = requirement.kind
    if kind == "tag":
        return lists["tags"]
    if kind == "custom_field":
        return lists["custom_fields"]
    if kind == "sequence":
        return lists["sequences"]
    if kind == "flow":
        return lists["flows"]
    if kind == "member":
        return lists["members"]
    if kind == "platform":
        return [row for row in lists["connections"] if row.get("platform") == requirement.key]
    if kind == "segment":
        model = installed_model("contacts", "apps.contacts", "Segment")
        if model is None:
            return []
        return [
            {"id": str(row["id"]), "label": row["name"]}
            for row in model.objects.for_workspace(workspace).order_by("name").values("id", "name")
        ]
    if kind == "media":
        model = installed_model("media_library", "apps.media_library", "MediaAsset")
        if model is None:
            return []
        return [
            {"id": str(row["id"]), "label": row["filename"]}
            for row in model.objects.for_workspace(workspace)
            .order_by("-created_at")
            .values("id", "filename")[:MEDIA_OPTIONS]
        ]
    return []


def _field_types() -> list[tuple[str, str]]:
    """The types a "create it" answer may pick for a new custom field.

    Read off the model's own choices so the wizard cannot offer one
    ``create_custom_field`` would refuse.
    """
    from apps.contacts.models import CustomFieldType

    return list(CustomFieldType.choices)


_KIND_LABELS: dict[str, str] = {
    "tag": "Tags",
    "custom_field": "Custom fields",
    "sequence": "Sequences",
    "segment": "Segments",
    "member": "Members",
    "flow": "Other flows",
    "media": "Media",
    "platform": "Channels",
    "request_header": "Request headers",
    "whatsapp_template": "WhatsApp templates",
    "link_handle": "Ref link handles",
    "from_override": "Email sender addresses",
    "comment_posts": "Comment trigger posts",
}

_KIND_HELP: dict[str, str] = {
    "tag": "Create them here, or point each one at a tag you already use.",
    "custom_field": "A new field needs a type; pick the one the template expects.",
    "sequence": "A new sequence arrives empty — add its steps afterwards.",
    "segment": "A segment is a saved filter and cannot be created from a template. Pick one you already have.",
    "member": "Who the flow assigns conversations to and notifies. Defaults to you.",
    "flow": "Flows this one hands over to. A bundle export carries them with it.",
    "media": "Pick an asset from your library, or paste a URL to use instead.",
    "platform": (
        "Which connection each trigger should watch. Leaving one unbound does not mean "
        "“every connection of this platform” — it means every platform that trigger type supports "
        "(SPEC §5), so a Telegram keyword trigger would also listen on SMS."
    ),
    "request_header": "Header values were removed on export so no credential could travel. Supply your own.",
    "whatsapp_template": "The flow sends these approved templates. Nothing to answer — make sure you have them.",
    "link_handle": "The public handle a ref link is built from was removed on export.",
    "from_override": "The sending address was removed on export.",
    "comment_posts": (
        "The trigger watched specific posts and their ids were removed on export. List your own — "
        "leaving it blank keeps the trigger scoped to specific posts with none listed, so it matches nothing."
    ),
}


def _mapping_from(request: WorkspaceRequest, record: FlowImport) -> dict[str, Any]:
    """Read the form back into a mapping.

    Field names are ``<kind>|<requirement key>|<field>``. The keys are compared
    against the requirements this document actually raises, so a hand-posted
    field naming a requirement that does not exist is dropped rather than stored
    — the same mass-assignment discipline the schemas apply to a graph
    (SECURITY-BASELINE §7).
    """
    wanted = {(requirement.kind, requirement.key) for requirement in portability.requirements_for(record.document)}
    # Triggers are not requirements — nothing has to be supplied for one — but
    # they are skippable, and they ride in the same dictionary so the wizard has
    # one form and one parser.
    wanted |= {(portability.TRIGGER_KIND, choice.key) for choice in portability.trigger_choices(record.document)}
    mapping: dict[str, dict[str, Any]] = {}
    for name, value in request.POST.items():
        parts = name.split("|", 2)
        if len(parts) != 3:
            continue
        kind, key, field = parts
        if (kind, key) not in wanted or field not in ("action", "id", "name", "field_type", "url", "value"):
            continue
        mapping.setdefault(kind, {}).setdefault(key, {})[field] = str(value)[:2000]
    return mapping


# ---------------------------------------------------------------------------
# Import, step three: confirm
# ---------------------------------------------------------------------------


@login_required
@require_permission("edit_flows")
@require_POST
def import_confirm(request: WorkspaceRequest, workspace_id: str, flow_import_id: str) -> HttpResponse:
    """Create the flows. The first write of the whole wizard.

    ``apply_import`` re-runs the dry run inside its own transaction rather than
    trusting the plan the review page rendered: the workspace may have changed
    since, and "the tag you mapped to has been deleted" must be a refusal rather
    than something discovered halfway through writing a graph.
    """
    record = get_scoped_object_or_404(FlowImport, request.workspace, pk=flow_import_id)
    if record.status == FlowImportStatus.APPLIED:
        return toast_response(tone="info", title="Already imported", body="This file has already been imported.")

    try:
        # ``confirm_import`` takes the row's lock and commits the flows and the
        # status transition together, so a double-clicked button imports once.
        # The check above is only a cheap early exit; it is not the guard.
        flows = portability.confirm_import(record, user=request.user)
    except portability.ImportNotReadyError as exc:
        return toast_response(tone="error", title="Not ready to import", body=_first_problem(exc.plan))
    except portability.ImportRefusedError as exc:
        return toast_response(tone="error", title="Nothing was imported", body=str(exc))
    except (ContactsError, CampaignsError) as exc:
        # The dry run checks every name and type before we get here, so this is
        # the narrow race: something the mapping named was created, renamed or
        # deleted between the review and the confirm. The transaction has rolled
        # back, so nothing partial exists — say so and let them look again.
        logger.info("Workspace %s could not apply import %s: %s", request.workspace.pk, record.pk, exc)
        return toast_response(
            tone="error",
            title="Nothing was imported",
            body=f"{exc} Re-check your answers and try again.",
        )

    if flows is None:
        # Somebody else confirmed it between the check above and the lock.
        return toast_response(tone="info", title="Already imported", body="This file has already been imported.")

    record.refresh_from_db()
    logger.info("Workspace %s imported %s flow(s) from %r", request.workspace.pk, len(flows), record.original_filename)
    return toast_response(
        tone="success",
        title="Imported as drafts",
        body=f"{len(flows)} flow(s) arrived unpublished, with their triggers switched off.",
        events={"flowsChanged": True, "flowImportApplied": True},
    )


def _first_problem(plan: portability.ImportPlan) -> str:
    unanswered = plan.unanswered
    if not unanswered:  # pragma: no cover - ImportNotReadyError implies at least one
        return "Something still has to be answered."
    first = unanswered[0]
    label = first.requirement.name or first.requirement.key
    return f"{label}: {first.problem}"


@login_required
@require_permission("edit_flows")
@require_POST
def import_discard(request: WorkspaceRequest, workspace_id: str, flow_import_id: str) -> HttpResponse:
    """Throw the upload away. Applied imports are kept as the record of what ran."""
    record = get_scoped_object_or_404(FlowImport, request.workspace, pk=flow_import_id)
    if record.status == FlowImportStatus.APPLIED:
        return toast_response(tone="info", title="Already imported", body="An applied import is kept as a record.")
    record.delete()
    return toast_response(tone="info", title="Import discarded", events={"flowImportDiscarded": True})
