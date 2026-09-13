"""Workspace-scoped views.

Every route here takes ``workspace_id``, which is ``RBACMiddleware``'s entire
resolution contract: by the time a view body runs, ``request.workspace`` and
``request.workspace_membership`` are set, and a workspace the user cannot reach
has already answered 404.
"""

from typing import Any

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.validators import is_valid_hex_color
from apps.flows.portability.library import gallery_entries
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest
from apps.workspaces.models import Workspace


@login_required
@require_GET
def dashboard(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The workspace landing page. Every role can see it.

    The KPI cards are issue #26's, and they are fetched through
    :func:`apps.analytics.selectors.dashboard_kpis` — a late import, because
    ``apps.analytics`` sits far above this app and a deployment without it must
    still have a landing page. No cards is a degraded dashboard; an ImportError
    is no dashboard at all.
    """
    # select_related and the archived filter belong here, not in the template:
    # iterating memberships and touching .workspace costs one query each, on the
    # most-visited page in the app.
    switchable = (
        WorkspaceMembership.objects.filter(user=request.user, workspace__is_archived=False)
        .select_related("workspace")
        .order_by("workspace__name")
    )
    permissions = request.workspace_membership.effective_permissions
    templates = gallery_entries()
    setup_steps = _setup_steps(request)
    setup_done = sum(1 for step in setup_steps if step["done"])
    return render(
        request,
        "workspaces/dashboard.html",
        {
            "switchable_memberships": switchable,
            "kpis": _kpis(request),
            "greeting": _greeting(),
            "setup_steps": setup_steps,
            "setup_done": setup_done,
            "setup_remaining": len(setup_steps) - setup_done,
            "setup_percent": round(setup_done / len(setup_steps) * 100),
            # The whole panel goes away for good once setup is finished. A
            # permanent "you have done everything" card is furniture.
            "setup_incomplete": setup_done < len(setup_steps),
            "needs_you": _needs_you(request),
            # The shipped starter templates, four of them, with the real total
            # beside "Browse all" — the page counts what is in flow-templates/
            # rather than claiming a number. Bound once above: calling
            # gallery_entries() twice here read and revalidated every shipped
            # file twice.
            "flow_templates": _template_sample(templates),
            "flow_template_total": len(templates),
            # Each card links to the section that can explain its number, and a
            # link a viewer would be refused at is worse than no link — so the
            # cards that need a permission carry it rather than guessing from
            # the role.
            "can_view_analytics": permissions.get("view_analytics", False),
            "can_send_broadcasts": permissions.get("send_broadcasts", False),
            "can_edit_flows": permissions.get("edit_flows", False),
        },
    )


def _greeting() -> str:
    """Morning, afternoon or evening, in the server's local time.

    Not the viewer's: the User model carries a time zone, but Django is already
    activating it for this request, so ``localtime`` is the reader's clock
    wherever that is configured — and where it is not, the server's is a better
    guess than UTC.
    """
    hour = timezone.localtime().hour
    if hour < 12:
        return "Good morning"
    return "Good afternoon" if hour < 18 else "Good evening"


def _kpis(request: WorkspaceRequest) -> dict[str, Any] | None:
    """The dashboard's numbers, or ``None`` when they should not be shown.

    ``None`` for two reasons: no analytics app, or a member without
    ``view_analytics``. Every workspace role holds that key today
    (``apps.members.roles``), so the second is not currently reachable — it is
    written anyway, because a permission that is only enforced where it happens
    to matter is a permission nobody can safely narrow later.
    """
    if not apps.is_installed("apps.analytics"):
        return None
    if not request.workspace_membership.effective_permissions.get("view_analytics", False):
        return None
    from apps.analytics.selectors import dashboard_daily_messages, dashboard_kpis

    kpis = dashboard_kpis(request.workspace)
    series = dashboard_daily_messages(request.workspace)
    peak = max((day["messages_in"] for day in series), default=0)
    # The bar heights are worked out here rather than in the template: Django's
    # template language has no arithmetic, and the alternative is a widthratio
    # tag whose rounding nobody can predict. A flat-zero week gets flat bars
    # rather than a division by zero.
    kpis["daily_messages"] = [
        {**day, "height": round(day["messages_in"] / peak * 100) if peak else 0} for day in series
    ]
    return kpis


#: How many template tiles Home shows. Four, because the row is four wide.
TEMPLATE_TILES = 4


def _template_sample(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Four templates, spread across platforms rather than the first four.

    `gallery_entries` is sorted by platform so the full page reads in runs, and
    slicing that gave Home four Facebook Messenger tiles in a row — which looks
    like the only thing the product does. One per platform first, then whatever
    fills the row.
    """
    seen: set[str] = set()
    picked: list[dict[str, Any]] = []
    for entry in templates:
        if entry["platform_label"] not in seen:
            seen.add(entry["platform_label"])
            picked.append(entry)
    for entry in templates:
        if len(picked) >= TEMPLATE_TILES:
            break
        if entry not in picked:
            picked.append(entry)
    return picked[:TEMPLATE_TILES]


def _setup_steps(request: WorkspaceRequest) -> list[dict[str, Any]]:
    """The first-run checklist: connect a channel, build a flow, invite people.

    Read from what the workspace actually has rather than from a stored
    "onboarding state" field. Nothing to migrate, nothing to keep in sync, and
    a workspace that had a channel and lost it honestly goes back to step one.

    Ordered by dependency — a flow with no channel has nothing to reply on —
    and the first incomplete step is the one the page offers to do next.
    """
    from apps.channels.models import ChannelConnection
    from apps.flows.models import Flow, FlowStatus

    workspace = request.workspace
    has_channel = ChannelConnection.objects.for_workspace(workspace).exists()
    live_flows = Flow.objects.for_workspace(workspace).filter(status=FlowStatus.ACTIVE).count()
    teammates = WorkspaceMembership.objects.filter(workspace=workspace).count()

    steps = [
        {
            "key": "channel",
            "title": "Connect a channel",
            "body": "Wherever people already message you: Telegram, Instagram, WhatsApp or email.",
            "cta": "Connect a channel",
            "url": reverse("channels:list", kwargs={"workspace_id": workspace.id}),
            "done": has_channel,
            "done_body": "Messages arrive here now.",
        },
        {
            "key": "flow",
            "title": "Build your first flow",
            "body": "A template comes with its trigger and its replies already written.",
            "cta": "Pick a template",
            "url": reverse("flows:list", kwargs={"workspace_id": workspace.id}),
            "done": live_flows > 0,
            "done_body": "A flow is answering for you.",
        },
        {
            "key": "team",
            "title": "Invite your team",
            "body": "Share the inbox so every reply does not land on you.",
            "cta": "Invite people",
            "url": reverse("members:list"),
            "done": teammates > 1,
            "done_body": "Other people can answer too.",
        },
    ]
    # Exactly one step is "current": the first unfinished one. The rest are
    # either done or not yet reachable, and showing three live calls to action
    # is the thing the redesign is trying to stop.
    current = next((step for step in steps if not step["done"]), None)
    for index, step in enumerate(steps, start=1):
        step["number"] = index
        step["current"] = step is current
    return steps


def _needs_you(request: WorkspaceRequest) -> list[dict[str, Any]]:
    """The short list of things actually waiting on a person.

    Each row is a real state the product already models — a revoked channel, an
    unassigned conversation, a broadcast about to go out — rather than a
    notification feed. Empty is a good answer and renders nothing.
    """
    from apps.broadcasts.models import Broadcast, BroadcastStatus
    from apps.channels.models import ChannelConnection, ConnectionStatus
    from apps.messaging.models import Conversation, ConversationState

    workspace = request.workspace
    permissions = request.workspace_membership.effective_permissions
    items: list[dict[str, Any]] = []

    if permissions.get("manage_channels", False):
        for connection in ChannelConnection.objects.for_workspace(workspace).filter(
            status=ConnectionStatus.NEEDS_REAUTH
        )[:3]:
            items.append(
                {
                    "tone": "bad",
                    "title": f"{connection.display_name} needs reconnecting",
                    "body": "It stopped receiving messages. Reconnect to start again.",
                    "url": reverse(
                        "channels:detail",
                        kwargs={"workspace_id": workspace.id, "connection_id": connection.id},
                    ),
                }
            )

    if permissions.get("use_inbox", False):
        unassigned = (
            Conversation.objects.for_workspace(workspace)
            .filter(state=ConversationState.OPEN, assignee__isnull=True)
            .count()
        )
        if unassigned:
            items.append(
                {
                    "tone": "warn",
                    "title": f"{unassigned} conversation{'' if unassigned == 1 else 's'} unassigned",
                    "body": "Nobody has picked these up yet.",
                    "url": reverse("inbox:list", kwargs={"workspace_id": workspace.id}),
                }
            )

    if permissions.get("send_broadcasts", False):
        for broadcast in Broadcast.objects.for_workspace(workspace).filter(status=BroadcastStatus.SCHEDULED)[:2]:
            items.append(
                {
                    "tone": "quiet",
                    "title": f"“{broadcast.name}” is scheduled",
                    "body": "Review it before it goes.",
                    "url": reverse(
                        "broadcasts:detail",
                        kwargs={"workspace_id": workspace.id, "broadcast_id": broadcast.id},
                    ),
                }
            )
    return items


@login_required
@require_POST
def switch(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Make this the user's current workspace.

    POST rather than Studio's GET link: it writes ``last_workspace_id``, and a
    state-changing GET is both CSRF-exposed and prefetchable. The membership
    check has already happened in the middleware, so reaching here means the
    switch is legitimate — a role check would be wrong, since which workspace
    you are looking at is a personal preference, not workspace data.

    The write is done here rather than left to the middleware's own
    keep-in-sync side effect. That side effect is an optimisation for ordinary
    navigation; making the switcher depend on it would mean any future narrowing
    of it — skipping GETs, gating it behind a check — silently turns this view
    into a redirect that changes nothing.
    """
    request.user.last_workspace_id = request.workspace.pk
    request.user.save(update_fields=["last_workspace_id", "updated_at"])
    return redirect(reverse("workspaces:dashboard", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def settings_view(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    return render(request, "workspaces/settings.html")


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def update_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    workspace = request.workspace
    # Strip first, then reject: `or workspace.name` only catches an empty
    # string, so "   " would survive it and be stored as a nameless workspace.
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "A workspace needs a name.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    # (organization, name) is unique, so without this an ordinary rename onto a
    # sibling's name reaches the constraint and 500s. The create flow already
    # checks; this one has to as well.
    clash = Workspace.objects.for_org(workspace.organization_id).filter(name=name).exclude(pk=workspace.pk)
    if clash.exists():
        messages.error(request, "Another workspace in this organization already has that name.")
        return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
    workspace.name = name
    workspace.icon = (request.POST.get("icon") or "").strip()[:8]
    workspace.description = (request.POST.get("description") or "").strip()[:500]
    workspace.timezone = (request.POST.get("timezone") or "").strip()[:63]

    for field in ("primary_color", "secondary_color"):
        value = (request.POST.get(field) or "").strip()
        if not is_valid_hex_color(value):
            messages.error(request, "Colours must be a 6-digit hex value like #3B82F6.")
            return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
        setattr(workspace, field, value)

    workspace.save(
        update_fields=["name", "icon", "description", "timezone", "primary_color", "secondary_color", "updated_at"]
    )
    messages.success(request, "Workspace settings saved.")
    return redirect(reverse("workspaces:settings", kwargs={"workspace_id": workspace_id}))
