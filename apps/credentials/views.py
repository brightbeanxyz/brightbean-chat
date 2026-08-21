"""Workspace-level credential overrides (SPEC §4, top of the resolution chain).

Admin-only: ``manage_workspace_settings`` is held by the Admin role alone, and
this page writes the platform secrets an attacker would most want.

The page never renders a stored secret. It shows the masked form and the level
the active credentials came from, so an operator can tell "the override is
working" from "the org's credentials are still in force" without the values
being on screen (SECURITY-BASELINE §5).
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.common.platforms import Platform
from apps.credentials.forms import WorkspaceCredentialOverrideForm
from apps.credentials.models import CONFIGURABLE_PLATFORMS, WorkspaceCredentialOverride
from apps.credentials.resolution import resolve_platform_credentials
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

PLATFORM_LABELS = dict(Platform.choices)


def _overrides_by_platform(workspace: object) -> dict[str, WorkspaceCredentialOverride]:
    return {o.platform: o for o in WorkspaceCredentialOverride.objects.for_workspace(workspace)}


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def credential_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    overrides = _overrides_by_platform(request.workspace)
    rows = []
    for platform in CONFIGURABLE_PLATFORMS:
        override = overrides.get(platform)
        resolution = resolve_platform_credentials(platform, workspace=request.workspace)
        rows.append(
            {
                "platform": platform,
                "label": PLATFORM_LABELS.get(platform, platform),
                "override": override,
                "masked": override.masked_credentials if override else {},
                "source": resolution.source,
                "configured": bool(resolution),
            }
        )
    return render(request, "credentials/list.html", {"rows": rows})


@login_required
@require_permission("manage_workspace_settings")
def edit_override(request: WorkspaceRequest, workspace_id: str, platform: str) -> HttpResponse:
    if platform not in CONFIGURABLE_PLATFORMS:
        from django.http import Http404

        raise Http404("No such platform.")

    override = WorkspaceCredentialOverride.objects.for_workspace(request.workspace).filter(platform=platform).first()
    instance = override or WorkspaceCredentialOverride(workspace=request.workspace, platform=platform)

    if request.method == "POST":
        form = WorkspaceCredentialOverrideForm(request.POST, instance=instance, platform=platform)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.workspace = request.workspace
            obj.platform = platform
            obj.save()
            messages.success(request, f"Saved the {PLATFORM_LABELS.get(platform, platform)} override.")
            return redirect(reverse("credentials:list", kwargs={"workspace_id": workspace_id}))
    else:
        # Never pre-fill the textarea with the stored secret. Re-entering the
        # whole credential set is the cost of not rendering it.
        form = WorkspaceCredentialOverrideForm(instance=instance, platform=platform, initial={"credentials": {}})

    return render(
        request,
        "credentials/edit.html",
        {
            "form": form,
            "platform": platform,
            "label": PLATFORM_LABELS.get(platform, platform),
            "override": override,
            "masked": override.masked_credentials if override else {},
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def clear_override(request: WorkspaceRequest, workspace_id: str, platform: str) -> HttpResponse:
    WorkspaceCredentialOverride.objects.for_workspace(request.workspace).filter(platform=platform).delete()
    messages.success(request, "Override removed. The organization or deployment credentials apply again.")
    return redirect(reverse("credentials:list", kwargs={"workspace_id": workspace_id}))
