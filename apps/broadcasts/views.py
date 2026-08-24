"""The broadcast pages: list, composer wizard and the live detail (SPEC §13).

Everything is gated on ``send_broadcasts``, reads included. A broadcast page
shows an audience and its compliance verdicts, which is the same material the
CRM gates behind ``manage_crm`` — and the placeholder route this replaces was
already gated on that key, so the permission a member needs to reach
``/broadcasts/`` does not change with this issue.

Decorator order is CONTRIBUTING.md's, outermost first, and it is load-bearing:
``@require_POST`` innermost means a cross-tenant GET answers 404 rather than 405,
and a 405 would confirm the route and the object exist.

--------------------------------------------------------------------------
The wizard is four server-rendered steps, not a client state machine
--------------------------------------------------------------------------

SPEC §13.1's four steps — channel, audience, content, schedule — are four
partials. Each one's form posts to its own endpoint, which saves through
:mod:`apps.broadcasts.services` and answers with the *next* step's markup. The
step an operator lands on is derived from what the row already holds, so a
half-finished draft reopens where it was left and nothing about "where am I" is
kept in the browser.

The audience step embeds ``templates/contacts/_filter_bar.html`` and
``apps.contacts.builder.builder_config`` verbatim — the payload module L6-C
extracted when the inbox-rule editor became the second page to render that
builder. A second condition builder here would be a second list of sources and
operators, and the whole point of contract 8 is that a broadcast audience, an
inbox rule and a saved segment agree by construction.

--------------------------------------------------------------------------
The detail page polls, and 304s
--------------------------------------------------------------------------

SPEC §14's transport, reused: :func:`counters` computes the figures, builds a
weak ETag **out of them**, and hands both to ``apps.common.polling.conditional``.
The counters *are* the state, so an unchanged poll never renders the fragment it
would have thrown away. The client half — htmx does not send ``If-None-Match``,
and htmx 2 swaps on 304 unless told not to — lives in ``templates/broadcasts/``
and is the same code ``templates/inbox/list.html`` documents.
"""

import json
from typing import Any

from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.broadcasts import audience as audience_module
from apps.broadcasts import composer as composer_module
from apps.broadcasts import services
from apps.broadcasts.models import Broadcast, BroadcastRecipient, BroadcastStatus, RecipientStatus
from apps.channels.models import ChannelConnection, WhatsAppTemplate
from apps.common.htmx import toast_response
from apps.common.polling import conditional, version_etag
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts.builder import builder_config
from apps.contacts.conditions import ConditionError
from apps.contacts.filters import parse_filter_document
from apps.contacts.models import Segment
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest
from apps.messaging.codes import describe

__all__ = [
    "STEPS",
    "audience_preview",
    "broadcast_cancel",
    "broadcast_create",
    "broadcast_delete",
    "broadcast_detail",
    "broadcast_duplicate",
    "broadcast_list",
    "broadcast_rows",
    "compose",
    "counters",
    "recipients",
    "save_audience",
    "save_channel",
    "save_content",
    "save_schedule",
    "wizard",
]

#: The composer's steps, in order (SPEC §13.1). A key into this tuple is the
#: only thing a request may say about which step it wants — a template name
#: built from a request parameter is a template-injection hole.
STEPS: tuple[str, ...] = ("channel", "audience", "content", "schedule")

#: How many broadcasts the list shows at once. The search and status filters
#: narrow within the whole set, so the cap bites only on the unfiltered view —
#: and the page says when it has.
PAGE_SIZE = 200

#: Cap on the composed message document. The flow schema applies its own size and
#: depth caps (``apps.flows.schema.envelope``), but those run *after* a parse, and
#: SECURITY-BASELINE §7 wants a body-size bound before the DB is touched.
MAX_CONFIG_BYTES = 64 * 1024

require_broadcasts = require_permission("send_broadcasts")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def _visible(request: WorkspaceRequest) -> Any:
    """The workspace's broadcasts, filtered by the toolbar.

    An unrecognised ``?status=`` falls back to no status filter rather than to
    no filtering at all — the trap ``apps.flows.views._visible_flows`` documents,
    where an ``if``/``elif`` let a bogus value skip the branch entirely.
    """
    rows = Broadcast.objects.for_workspace(request.workspace).select_related("channel_connection")
    term = (request.GET.get("q") or "").strip()[:200]
    if term:
        rows = rows.filter(name__icontains=term)
    status = (request.GET.get("status") or "").strip()
    if status in BroadcastStatus.values:
        rows = rows.filter(status=status)
    return rows.order_by("-created_at")


def _rows_context(request: WorkspaceRequest) -> dict[str, Any]:
    # One row past the cap, so the page can *say* it truncated rather than
    # quietly showing a workspace's newest two hundred and letting an operator
    # conclude the older ones were deleted. A silent cap is the failure mode
    # worth spending one extra row on.
    page = list(_visible(request)[: PAGE_SIZE + 1])
    return {
        "broadcasts": page[:PAGE_SIZE],
        "truncated": len(page) > PAGE_SIZE,
        "page_size": PAGE_SIZE,
        "q": (request.GET.get("q") or "").strip()[:200],
        "status": (request.GET.get("status") or "").strip(),
        "status_options": list(BroadcastStatus.choices),
    }


@login_required
@require_broadcasts
@require_GET
def broadcast_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The index: a toolbar, and the rows partial underneath it."""
    context = {
        **_rows_context(request),
        "connections": composer_module.broadcastable_connections(request.workspace),
    }
    return render(request, "broadcasts/list.html", context)


@login_required
@require_broadcasts
@require_GET
def broadcast_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The table body on its own, so the toolbar and every mutation share one renderer."""
    return render(request, "broadcasts/_rows.html", _rows_context(request))


@login_required
@require_broadcasts
@require_POST
def broadcast_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Start a draft on one connection and open the composer on it."""
    connection = _connection(request, request.POST.get("connection_id"))
    name = (request.POST.get("name") or "").strip() or "Untitled broadcast"
    try:
        broadcast = services.create_broadcast(
            workspace=request.workspace, name=name, connection=connection, user=request.user
        )
    except services.BroadcastError as exc:
        return toast_response(tone="error", title="Could not create it", body=str(exc))
    return _redirect(reverse("broadcasts:compose", kwargs={"workspace_id": workspace_id, "broadcast_id": broadcast.pk}))


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _step_for(broadcast: Broadcast) -> str:
    """The first step this draft has not answered yet.

    Derived from the row rather than tracked in the session, so reopening a
    half-finished draft lands where it was left and two tabs cannot disagree.
    """
    if broadcast.channel_connection_id is None:
        return "channel"
    if not broadcast.target_filter_json:
        return "audience"
    if broadcast.flow_id is None and broadcast.whatsapp_template_id is None:
        return "content"
    return "schedule"


def _requested_step(request: WorkspaceRequest, broadcast: Broadcast) -> str:
    """The step to render: whichever the request asks for, if it is one of ours.

    Membership in :data:`STEPS` is checked rather than trusted, because the value
    picks a template name.
    """
    wanted = (request.GET.get("step") or "").strip()
    return wanted if wanted in STEPS else _step_for(broadcast)


def _wizard_context(request: WorkspaceRequest, broadcast: Broadcast, step: str, **extra: Any) -> dict[str, Any]:
    connection = broadcast.channel_connection
    context: dict[str, Any] = {
        "broadcast": broadcast,
        "step": step,
        "steps": STEPS,
        "reached": _step_for(broadcast),
        "composer": composer_module.composer_config(request.workspace, connection),
        "connections": composer_module.broadcastable_connections(request.workspace),
    }
    if step == "audience":
        # L6-C extracted this payload into apps/contacts/builder.py when the
        # inbox-rule editor became the second page to embed the same filter bar.
        # Three consumers now, one payload — which is the point: an operator
        # added to apps.contacts.conditions reaches all of them with no edit.
        context["filter_config"] = builder_config(
            request.workspace,
            document=broadcast.target_filter_json or {},
            segment_id=str(broadcast.segment_id) if broadcast.segment_id else "",
        )
        # The same fragment the preview endpoint renders, filled server-side so
        # the count is on screen before the first poll rather than a beat later.
        # Skipped for a draft with no audience yet, where there is nothing to
        # count and the aggregate would be three queries for an empty answer.
        preview = audience_module.preview(broadcast) if broadcast.target_filter_json else None
        context["preview"] = preview
        context["reasons"] = _reasons(preview.skipped, preview.samples) if preview else []
    if step == "content":
        context["config"] = services.node_config(_graph_of(broadcast))
    context.update(extra)
    return context


def _graph_of(broadcast: Broadcast) -> Any:
    from apps.flows.services import latest_version

    if broadcast.flow is None:
        return {}
    version = latest_version(broadcast.flow)
    return version.graph_json if version is not None else {}


@login_required
@require_broadcasts
@require_GET
def compose(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """The composer page. A sent or sending broadcast redirects to its detail."""
    broadcast = _broadcast(request, broadcast_id)
    if broadcast.status != BroadcastStatus.DRAFT:
        # A real 302, not ``HX-Redirect``. This is a page, reached by typing or
        # bookmarking a URL as often as by clicking, and a browser with no htmx
        # in the request ignores that header completely — the 204 would render as
        # a blank page and go nowhere. The htmx POST endpoints below are the
        # opposite case and keep ``_redirect``.
        return redirect("broadcasts:detail", workspace_id=workspace_id, broadcast_id=broadcast.pk)
    step = _requested_step(request, broadcast)
    return render(request, "broadcasts/compose.html", _wizard_context(request, broadcast, step))


@login_required
@require_broadcasts
@require_GET
def wizard(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """One step's markup, for the back/next links to swap in."""
    broadcast = _broadcast(request, broadcast_id)
    step = _requested_step(request, broadcast)
    return render(request, "broadcasts/_wizard.html", _wizard_context(request, broadcast, step))


def _advance(request: WorkspaceRequest, broadcast: Broadcast, step: str, **extra: Any) -> HttpResponse:
    """Render one step back into the wizard container."""
    broadcast.refresh_from_db()
    return render(request, "broadcasts/_wizard.html", _wizard_context(request, broadcast, step, **extra))


@login_required
@require_broadcasts
@require_POST
def save_channel(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    connection = _connection(request, request.POST.get("connection_id"))
    try:
        services.set_channel(broadcast, connection)
    except services.BroadcastError as exc:
        return _refused(request, broadcast, "channel", exc)
    return _advance(request, broadcast, "audience")


@login_required
@require_broadcasts
@require_POST
def save_audience(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    try:
        document, segment = _audience_from(request)
        services.set_audience(broadcast, filter_json=document, segment=segment)
    except (ConditionError, services.BroadcastError) as exc:
        return _refused(request, broadcast, "audience", exc)
    return _advance(request, broadcast, "content")


@login_required
@require_broadcasts
@require_POST
def save_content(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """Store the composed message — a single-node graph, or a template mapping.

    Which of the two is decided by the form's ``mode`` field rather than by the
    platform: a WhatsApp broadcast may perfectly well be an ordinary message
    inside the window, and the composer offers whichever the connection's
    registries said were available.
    """
    broadcast = _broadcast(request, broadcast_id)
    try:
        if (request.POST.get("mode") or "message") == "template":
            template = get_scoped_object_or_404(
                WhatsAppTemplate, request.workspace, pk=request.POST.get("template_id") or ""
            )
            services.save_template(broadcast, template, _json_field(request, "variables"))
        else:
            services.save_content(broadcast, _json_field(request, "config"), user=request.user)
        services.set_tag(broadcast, request.POST.get("message_tag") or "")
    except services.BroadcastError as exc:
        return _refused(request, broadcast, "content", exc)
    except ValueError as exc:
        return _refused(request, broadcast, "content", exc)
    return _advance(request, broadcast, "schedule")


@login_required
@require_broadcasts
@require_POST
def save_schedule(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """Send now or later. This is the button that puts the fanout in the queue."""
    broadcast = _broadcast(request, broadcast_id)
    try:
        when = _when(request)
    except ValueError as exc:
        return _refused(request, broadcast, "schedule", exc)
    try:
        services.schedule_broadcast(broadcast, when=when)
    except services.BroadcastError as exc:
        return _refused(request, broadcast, "schedule", exc)
    return _redirect(reverse("broadcasts:detail", kwargs={"workspace_id": workspace_id, "broadcast_id": broadcast.pk}))


@login_required
@require_broadcasts
@require_GET
def audience_preview(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """The live count, computed against a filter the operator has not saved yet.

    Set-wise (three aggregates, whatever the audience size) so a keystroke in the
    filter bar costs the same on ten contacts and on ten thousand.

    The document is *not* stored: this endpoint answers "who would this reach",
    and saving on a GET would make a preview a mutation.
    """
    broadcast = _broadcast(request, broadcast_id)
    try:
        document, segment = _audience_from(request, params=request.GET)
    except ConditionError as exc:
        return render(request, "broadcasts/_audience_preview.html", {"error": str(exc)})

    if not document:
        # No rules yet — which is what the page loads with, and what the filter
        # bar serialises to while it is empty. An empty document is not a filter
        # the condition engine can compile ("missing key(s): match, rules"), so
        # asking it would be a 500 on a keystroke; there is also nothing to
        # count. ``{"match": "all", "rules": []}`` is a different thing entirely
        # and does reach the engine: it targets everyone, deliberately, which is
        # exactly why this preview exists.
        return render(request, "broadcasts/_audience_preview.html", {"preview": None})

    # A throwaway instance so the preview is computed against the filter on
    # screen rather than the one on disk. Never saved — assigning to an unsaved
    # copy keeps that impossible to get wrong by accident.
    probe = Broadcast(
        workspace=broadcast.workspace,
        channel_connection=broadcast.channel_connection,
        target_filter_json=document,
        message_tag=request.GET.get("message_tag") or broadcast.message_tag,
        whatsapp_template=broadcast.whatsapp_template,
    )
    preview = audience_module.preview(probe)
    return render(
        request,
        "broadcasts/_audience_preview.html",
        {"preview": preview, "reasons": _reasons(preview.skipped, preview.samples), "segment": segment},
    )


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


def _clicked(broadcast: Broadcast) -> int:
    """How many clicks the broadcast's links have taken (issue #26).

    A broadcast's content is a private one-node mini-flow, so its clicks are that
    node's row in ``node_stat_daily`` — the same counter the flow builder's
    overlay reads, reached the same way. This app keeps no click count of its
    own, exactly as it keeps no delivery-receipt path of its own.

    A late import behind an installed check: ``apps.analytics`` sits above this
    app, and a deployment without it should lose one tile rather than the page.
    """
    if broadcast.flow_id is None or not apps.is_installed("apps.analytics"):
        return 0
    from apps.analytics.selectors import node_clicks

    return node_clicks(broadcast.workspace_id, broadcast.flow_id, services.CONTENT_NODE_ID)


def _counters_context(broadcast: Broadcast) -> dict[str, Any]:
    current = services.counters(broadcast)
    return {
        "broadcast": broadcast,
        "counters": current,
        "clicked": _clicked(broadcast),
        # While fanout is still expanding the audience, ``queued`` is the number
        # of recipients written *so far* rather than the total — so a percentage
        # against it walks backwards every time a chunk of five hundred lands.
        # The bar is indeterminate until the denominator is real.
        "expanding": services.fanout_outstanding(broadcast),
        "reasons": _reasons(current.skips),
    }


@login_required
@require_broadcasts
@require_GET
def broadcast_detail(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    return render(
        request,
        "broadcasts/detail.html",
        {**_counters_context(broadcast), "deliverability": _deliverability(request, broadcast)},
    )


def _deliverability(request: WorkspaceRequest, broadcast: Broadcast) -> dict[str, Any] | None:
    """How this broadcast's channel is delivering overall (issue #26).

    Everything the connection has ever sent, not just this broadcast — the
    counters above already answer "how did this send go", and the question this
    answers is the different one beside it: is the channel healthy. A domain
    whose delivery rate has been falling for a week is the thing an operator
    wants to see before scheduling the next one.

    Computed in the page view only, never in the three-second polled fragment: it
    is a workspace-wide aggregate and it does not move while somebody watches.
    """
    if not apps.is_installed("apps.analytics"):
        return None
    from apps.analytics.selectors import connection_deliverability, resolve_range

    rows = connection_deliverability(request.workspace, window=resolve_range(None))
    return next((row for row in rows if row["connection"].pk == broadcast.channel_connection_id), None)


@login_required
@require_broadcasts
@require_GET
def counters(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """The progress fragment, polled every 3 s. 304 when nothing moved.

    The token is built from the figures the fragment renders, not from a row's
    ``updated_at``: the counters *are* what changed, so a tag made of them cannot
    disagree with the markup — the property ``apps.common.polling``'s docstring
    asks a caller to arrange.
    """
    broadcast = _broadcast(request, broadcast_id)
    context = _counters_context(broadcast)
    current = context["counters"]
    etag = version_etag(
        "broadcast-counters",
        broadcast.pk,
        broadcast.status,
        current.queued,
        current.pending,
        current.sent,
        current.delivered,
        current.read,
        current.failed,
        current.skipped,
        current.cancelled,
        # Clicks move without any recipient row changing, so the tag has to carry
        # them too or the fragment would 304 while the tile it renders is stale.
        context["clicked"],
    )

    def build() -> HttpResponse:
        # Materialise the figures onto the row while they are in hand, so the
        # list page and the finished-event payload have them without recomputing.
        # Inside `build` rather than beside it: a 304 has nothing new to store,
        # and writing on every poll is what "updated in batches" rules out.
        services.release_stats(broadcast, current=current)
        return render(request, "broadcasts/_counters.html", context)

    return conditional(request, etag, build)


@login_required
@require_broadcasts
@require_GET
def recipients(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    """Who was skipped and why — ``annotate_eligibility``'s verdicts, per person.

    Bounded to one page: a ten-thousand-recipient broadcast has a counter for the
    whole and a list for the part an operator can act on.
    """
    broadcast = _broadcast(request, broadcast_id)
    status = (request.GET.get("status") or RecipientStatus.SKIPPED).strip()
    if status not in RecipientStatus.values:
        status = RecipientStatus.SKIPPED
    rows = (
        BroadcastRecipient.objects.for_workspace(request.workspace)
        .filter(broadcast=broadcast, status=status)
        .select_related("contact")
        .order_by("contact__first_name", "contact__last_name", "pk")[:100]
    )
    return render(
        request,
        "broadcasts/_recipients.html",
        {"broadcast": broadcast, "rows": rows, "status": status, "status_options": list(RecipientStatus.choices)},
    )


@login_required
@require_broadcasts
@require_POST
def broadcast_cancel(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    try:
        services.cancel_broadcast(broadcast)
    except services.BroadcastError as exc:
        return toast_response(tone="error", title="Could not cancel it", body=str(exc))
    return toast_response(
        tone="success",
        title="Broadcast cancelled",
        body="Sends that had not gone out yet were stopped.",
        events={"broadcastChanged": True},
    )


@login_required
@require_broadcasts
@require_POST
def broadcast_duplicate(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    try:
        copy = services.duplicate_broadcast(broadcast, user=request.user)
    except services.BroadcastError as exc:
        return toast_response(tone="error", title="Could not duplicate it", body=str(exc))
    return _redirect(reverse("broadcasts:compose", kwargs={"workspace_id": workspace_id, "broadcast_id": copy.pk}))


@login_required
@require_broadcasts
@require_POST
def broadcast_delete(request: WorkspaceRequest, workspace_id: str, broadcast_id: str) -> HttpResponse:
    broadcast = _broadcast(request, broadcast_id)
    try:
        services.delete_broadcast(broadcast)
    except services.BroadcastError as exc:
        return toast_response(tone="error", title="Could not delete it", body=str(exc))
    return toast_response(tone="success", title="Broadcast deleted", events={"broadcastsChanged": True})


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


def _broadcast(request: WorkspaceRequest, broadcast_id: str) -> Broadcast:
    """One broadcast, or 404 — never 403, whether it is another tenant's or absent."""
    return get_scoped_object_or_404(Broadcast, request.workspace, pk=broadcast_id)


def _connection(request: WorkspaceRequest, connection_id: Any) -> ChannelConnection:
    """Resolve a connection, and refuse one broadcasts are not allowed on.

    404 rather than a validation error for a platform that forbids broadcasts:
    it was never offered by the selector, so a request naming it is not a form
    an operator could have submitted — and SPEC §13.2's "Instagram never appears
    in the broadcast channel selector" is only true if the endpoint agrees with
    the selector.
    """
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id or "")
    if connection not in composer_module.broadcastable_connections(request.workspace):
        raise Http404("That channel cannot be used for broadcasts.")
    return connection


def _audience_from(request: WorkspaceRequest, params: Any = None) -> tuple[dict[str, Any], Segment | None]:
    """``?segment=`` or ``?filter=``, resolved the way the CRM resolves them.

    A segment wins over a raw filter when both are present — loading a segment is
    an explicit act and the stale ``filter`` left behind by the control that
    loaded it is not — and the id goes through ``get_scoped_object_or_404``, so
    another workspace's segment is a 404 rather than an empty audience. The IDOR
    sweep walks URL kwargs and cannot see a query-string id, so that case has its
    own test.
    """
    params = params if params is not None else request.POST
    segment_id = (params.get("segment") or "").strip()
    if segment_id:
        segment = get_scoped_object_or_404(Segment, request.workspace, pk=segment_id)
        document = segment.filter_json if isinstance(segment.filter_json, dict) else {}
        return document, segment
    return parse_filter_document(params.get("filter")), None


def _json_field(request: WorkspaceRequest, name: str) -> dict[str, Any]:
    """One form field carrying a JSON document, size-capped before it is parsed.

    The composer serialises its whole state into a single hidden input, the same
    way ``templates/contacts/_filter_bar.html`` does — posting the blocks as
    separate form fields would mean re-assembling the document server-side, which
    is a second parser for a language that already has one. The cap runs before
    ``json.loads`` because SECURITY-BASELINE §7 wants a bound before the work,
    and the flow schema's own size and depth caps then run over the result.
    """
    raw = request.POST.get(name) or ""
    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("That message is too large to store.")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ValueError("That message could not be read.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("That message could not be read.")
    return parsed


def _when(request: WorkspaceRequest) -> Any:
    """ "Now", or an instant parsed in the workspace's own timezone (SPEC §13.1).

    A naive value from the picker is interpreted in ``Workspace.effective_timezone``
    rather than the server's: an operator picking "09:00" means nine in the
    morning where they work, and a server in UTC would send it at whatever nine
    UTC happens to be for them.
    """
    if (request.POST.get("when") or "now") == "now":
        return timezone.now()
    raw = (request.POST.get("scheduled_at") or "").strip()
    parsed = parse_datetime(raw) if raw else None
    if parsed is None:
        raise ValueError("Pick a date and time to send.")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, _workspace_tz(request))
    if parsed < timezone.now():
        raise ValueError("That time has already passed.")
    return parsed


def _workspace_tz(request: WorkspaceRequest) -> Any:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(request.workspace.effective_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.get_current_timezone()


def _reasons(skips: dict[str, int], samples: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Skip codes turned into rows a person can read, biggest first.

    The sentence comes from ``apps.messaging.codes.describe`` — the registered
    copy the inbox already shows — rather than from a table in this app, so a
    code added by a later platform explains itself here with no edit.

    ``names`` is a handful of examples per reason. A count on its own tells an
    operator that something is wrong; a name tells them what to go and look at.
    """
    samples = samples or {}
    return [
        {"code": code, "label": describe(code), "count": count, "names": samples.get(code, [])}
        for code, count in sorted(skips.items(), key=lambda item: (-item[1], item[0]))
    ]


def _refused(request: WorkspaceRequest, broadcast: Broadcast, step: str, exc: Exception) -> HttpResponse:
    """Re-render the step with the reason attached, rather than a bare toast.

    The refusals here are all things the operator has to change on the form in
    front of them — a missing tag, an empty audience — so the message belongs
    next to the control, not in a toast that scrolls away.
    """
    return _advance(request, broadcast, step, error=str(exc))


def _redirect(url: str) -> HttpResponse:
    """A redirect htmx follows as a full-page navigation.

    ``HX-Redirect`` rather than a 302: htmx follows a 302 by swapping the new
    page's body into whatever made the request, which for a form inside the
    wizard means the whole page rendered inside a panel.
    """
    return HttpResponse(status=204, headers={"HX-Redirect": url})
