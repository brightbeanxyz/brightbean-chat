"""Organization-level settings.

Org routes carry no organization id in the URL: v1 is one organization per user
and ``RBACMiddleware`` resolves it (see that module's docstring on the
assumption and what changes if multi-org ever arrives).
"""

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.channels.models import ChannelConnection
from apps.members.decorators import require_org_role
from apps.members.models import WorkspaceMembership
from apps.members.requests import OrgRequest
from apps.members.roles import OrgRole, WorkspaceRole
from apps.workspaces.models import Workspace


def _can_manage(request: OrgRequest) -> bool:
    return request.org_membership.org_role in (OrgRole.OWNER, OrgRole.ADMIN)


@login_required
@require_org_role("member")
@require_GET
def settings_view(request: OrgRequest) -> HttpResponse:
    return render(request, "organizations/settings.html", {"can_manage": _can_manage(request)})


@login_required
@require_org_role("admin")
@require_POST
def update_settings(request: OrgRequest) -> HttpResponse:
    org = request.org
    # Strip first, then reject — see apps/workspaces/views.py for the same trap.
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "An organization needs a name.")
        return redirect(reverse("organizations:settings"))
    org.name = name
    org.default_timezone = (request.POST.get("default_timezone") or org.default_timezone).strip()[:63]
    org.logo_url = (request.POST.get("logo_url") or "").strip()
    org.save(update_fields=["name", "default_timezone", "logo_url", "updated_at"])
    messages.success(request, "Organization settings saved.")
    return redirect(reverse("organizations:settings"))


@login_required
@require_org_role("member")
@require_GET
def workspaces_view(request: OrgRequest) -> HttpResponse:
    """Every workspace in the org, archived ones included.

    This is the only place an archived workspace is visible: ``/w/<id>/`` 404s
    for them (``RBACMiddleware``), so without this list there is no way back.
    """
    workspaces = Workspace.objects.for_org(request.org.pk).order_by("is_archived", "name")
    member_workspace_ids = set(
        WorkspaceMembership.objects.filter(user=request.user, workspace__organization=request.org).values_list(
            "workspace_id", flat=True
        )
    )
    return render(
        request,
        "organizations/workspaces.html",
        {
            "workspaces": workspaces,
            "member_workspace_ids": member_workspace_ids,
            "can_manage": _can_manage(request),
        },
    )


@login_required
@require_org_role("admin")
@require_POST
def create_workspace(request: OrgRequest) -> HttpResponse:
    name = (request.POST.get("name") or "").strip()[:100]
    if not name:
        messages.error(request, "A workspace needs a name.")
        return redirect(reverse("organizations:workspaces"))
    if Workspace.objects.for_org(request.org.pk).filter(name=name).exists():
        messages.error(request, "A workspace with that name already exists.")
        return redirect(reverse("organizations:workspaces"))

    workspace = Workspace.objects.create(organization=request.org, name=name)
    # The creator becomes its admin, or nobody can configure the thing they
    # just made.
    WorkspaceMembership.objects.create(user=request.user, workspace=workspace, workspace_role=WorkspaceRole.ADMIN)
    messages.success(request, f"Created {workspace.name}.")
    return redirect(reverse("organizations:workspaces"))


@login_required
@require_org_role("admin")
@require_POST
def set_workspace_archived(request: OrgRequest, target_id: str) -> HttpResponse:
    """Archive or restore a workspace.

    The kwarg is ``target_id``, not ``workspace_id``, and that is load-bearing:
    ``workspace_id`` is ``RBACMiddleware``'s resolution contract, and the
    middleware 404s archived workspaces — so naming it that would make
    unarchiving impossible. Tenancy is enforced here instead, by scoping the
    lookup to ``request.org`` and answering 404 on a miss.
    """
    workspace = Workspace.objects.for_org(request.org.pk).filter(pk=target_id).first()
    if workspace is None:
        raise Http404("No such workspace.")

    workspace.is_archived = request.POST.get("archived") == "1"
    workspace.save(update_fields=["is_archived", "updated_at"])
    messages.success(request, f"{'Archived' if workspace.is_archived else 'Restored'} {workspace.name}.")
    return redirect(reverse("organizations:workspaces"))


def _first_of_next_month(day: date) -> date:
    """The first of the month after ``day``, in plain dates.

    Whole-date arithmetic rather than timedelta on an aware datetime: the only
    thing that varies between months is their length, and December has to roll
    the year. No clock, so no DST and no offset to get wrong.
    """
    return date(day.year + (day.month == 12), (day.month % 12) + 1, 1)


@login_required
@require_org_role("member")
@require_GET
def billing_view(request: OrgRequest) -> HttpResponse:
    """Plan and billing.

    **There is no billing app.** No plan model, no subscription, no Stripe
    customer — nothing in this repository charges anybody. So the page is
    honest about that: the usage figures are real, read from the same models
    everything else reads, and the plan comparison is marked on its face as not
    yet connected.

    The alternative was to leave the route stubbed behind "coming soon". That
    is worse: the numbers a workspace wants first — how many people you talked
    to, how many seats are in use, how many channels are connected — all exist
    today, and hiding them behind an unbuilt payment integration means nobody
    can answer "am I near a limit" until billing ships.

    Usage is counted across the organisation's workspaces rather than the
    current one: a plan is bought by an organisation, and a per-workspace number
    on a page headed "Plan" would be the wrong denominator.
    """
    from apps.contacts.models import Contact
    from apps.messaging.models import Conversation

    workspaces = Workspace.objects.filter(organization=request.org, is_archived=False)
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return render(
        request,
        "organizations/billing.html",
        {
            "can_manage": _can_manage(request),
            # "Talked to", not "has" — the figure a usage-based plan would bill
            # on is people you had a conversation with this month, and a total
            # contact count would be a much larger number meaning something
            # else entirely.
            # .unscoped(), with the reason CONTRIBUTING.md asks for: a plan is
            # bought by an ORGANISATION, so its usage is the sum across that
            # organisation's workspaces and a single-workspace figure would be
            # the wrong denominator on a page headed "Plan". Every query is
            # still bounded by `workspace__in=workspaces`, which is this org's
            # own list — this crosses workspaces, never tenants.
            "contacts_talked_to": (
                Conversation.objects.unscoped()
                .filter(workspace__in=workspaces, last_message_at__gte=month_start)
                .values("contact_id")
                .distinct()
                .count()
            ),
            "contacts_total": Contact.objects.unscoped().filter(workspace__in=workspaces).count(),
            "seats_in_use": (
                WorkspaceMembership.objects.filter(workspace__in=workspaces).values("user_id").distinct().count()
            ),
            "channels_connected": ChannelConnection.objects.unscoped().filter(workspace__in=workspaces).count(),
            # A date, not a datetime. Adding a timedelta to an aware datetime
            # and calling .replace(day=1) does naive arithmetic and keeps this
            # month's UTC offset, so across a DST boundary the result is an
            # hour off and carries the wrong tzinfo. Nothing but `|date` reads
            # it today, but a wrong instant that happens to render right is a
            # trap for whoever compares or stores it next.
            "renews_on": _first_of_next_month(month_start.date()),
        },
    )
