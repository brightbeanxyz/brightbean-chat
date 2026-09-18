"""Organization-level settings.

Org routes carry no organization id in the URL: v1 is one organization per user
and ``RBACMiddleware`` resolves it (see that module's docstring on the
assumption and what changes if multi-org ever arrives).
"""

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.billing.entitlements import organization_locked
from apps.common.windows import timezone_choices, zone
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
    return render(
        request,
        "organizations/settings.html",
        {
            "can_manage": _can_manage(request),
            "timezone_choices": timezone_choices(request.org.default_timezone),
        },
    )


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
    # Unlike a workspace's, this one is never blank — it is the fallback every
    # workspace without its own clock reads — so an empty post keeps the current
    # value rather than clearing it. See apps/workspaces/views.py for why the
    # check is zone() and not membership of timezone_choices().
    submitted_timezone = (request.POST.get("default_timezone") or org.default_timezone).strip()[:63]
    if zone(submitted_timezone) is None:
        messages.error(request, "That is not a timezone we recognise.")
        return redirect(reverse("organizations:settings"))
    org.default_timezone = submitted_timezone
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
@require_org_role("member")
@require_GET
def billing_view(request: OrgRequest) -> HttpResponse:
    """Plan and billing.

    ``member``, not ``admin``: everybody in the organization can see which plan
    they are on and how much of it is used. Only an admin gets the Subscribe and
    Manage billing controls, and those are the POSTs in ``apps.billing.views``,
    which gate themselves.

    **Always 200, including on a deployment with no Stripe.** The usage figures
    come from the same models everything else reads, and "how many people did we
    talk to this month" is worth answering whether or not anybody is selling
    anything. What an unconfigured deployment does not get is the plan
    comparison — see the template.

    Usage is counted across the organization's workspaces rather than the
    current one: a plan is bought by an organization, and a per-workspace figure
    on a page headed "Plan" would be the wrong denominator.
    """
    from apps.billing.selectors import billing_context

    context: dict[str, Any] = {"can_manage": _can_manage(request)}
    context.update(billing_context(request.org, checkout=request.GET.get("checkout", "")))
    return render(request, "organizations/billing.html", context)


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
    # The count and the insert are one critical section. See
    # apps/billing/entitlements.organization_locked: a lock released when the
    # check returns serialises nothing.
    with organization_locked(request.org):
        refusal = _plan_refusal(request.org)
        if refusal is not None:
            messages.error(request, refusal)
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

    archiving = request.POST.get("archived") == "1"
    if not archiving and workspace.is_archived:
        # Restoring is the same lever as creating. Without this an organization
        # at its workspace limit archives one, creates another, and restores the
        # first — which is the shape every "half-enforced limit" bug takes. The
        # write is inside the lock for the reason create_workspace's is.
        with organization_locked(request.org):
            refusal = _plan_refusal(request.org)
            if refusal is not None:
                messages.error(request, refusal)
                return redirect(reverse("organizations:workspaces"))
            workspace.is_archived = archiving
            workspace.save(update_fields=["is_archived", "updated_at"])
            messages.success(request, f"Restored {workspace.name}.")
            return redirect(reverse("organizations:workspaces"))

    workspace.is_archived = archiving
    workspace.save(update_fields=["is_archived", "updated_at"])
    messages.success(request, f"{'Archived' if workspace.is_archived else 'Restored'} {workspace.name}.")
    return redirect(reverse("organizations:workspaces"))


def _plan_refusal(org: Any) -> str | None:
    """The organization's workspace limit, as a message or None.

    Returns rather than raises: both call sites are POST-redirect-GET views that
    answer a refusal with ``messages.error`` and a redirect, which is what every
    other refusal in this module already does.

    A free plan is capped at one workspace, and that cap is what makes every
    other limit affordable to enforce — the counts in
    ``apps.billing.entitlements`` sum across an organization's workspaces, and
    for an organization that has never paid that sum has one term.
    """
    from apps.billing.entitlements import PlanLimitError, check_can_add_workspace

    try:
        check_can_add_workspace(org)
    except PlanLimitError as exc:
        return str(exc)
    return None
