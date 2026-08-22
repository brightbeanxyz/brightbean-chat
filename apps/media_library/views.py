"""Library browser, picker endpoint and the public delivery route.

Reads (the browser, the detail panel, the picker JSON) are gated on workspace
membership alone — ``RBACMiddleware`` already answers 404 for anyone who is not
a member, and an Agent who cannot upload still has to attach an existing asset
to an inbox reply (#24). Writes are gated on ``manage_media``.

Every object fetch goes through ``get_scoped_object_or_404``. Django's own
``get_object_or_404`` routes through ``_default_manager`` — the *plain* manager
on a tenant model — so it would look correct and cross tenants.

HTMX is detected from the ``HX-Request`` header rather than Studio's
``request.htmx``: there is no ``django-htmx`` here, the library is vendored JS.
"""

import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.media_library import services
from apps.media_library.delivery import delivery_response, read_token
from apps.media_library.filters import ROOT_FOLDER, filter_assets
from apps.media_library.mimes import MediaKind, UnsupportedMediaError, accepted_upload_types
from apps.media_library.models import MediaAsset, MediaFolder
from apps.media_library.picker import DEFAULT_LIMIT, picker_payload
from apps.media_library.quotas import QuotaExceededError, used_bytes, workspace_quota_bytes
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

GRID_PAGE_SIZE = 48


def _is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def _with_trigger(response: HttpResponse, triggers: dict[str, Any]) -> HttpResponse:
    """Attach ``HX-Trigger`` events to a response that also has a body.

    ``apps.common.htmx`` covers the bodiless case (204 plus events); an upload
    wants both — the refreshed grid *and* a toast naming what failed.
    """
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _toast(tone: str, title: str, body: str = "") -> dict[str, Any]:
    return {"showToast": {"tone": tone, "title": title, "body": body}}


def _folder_or_404(request: WorkspaceRequest, folder_id: Any) -> MediaFolder | None:
    if not folder_id or folder_id == ROOT_FOLDER:
        return None
    return get_scoped_object_or_404(MediaFolder, request.workspace, pk=folder_id)


def _folder_rows(workspace: Any) -> list[dict[str, Any]]:
    """The folder rail, in tree order with a depth for indentation.

    Indentation without tree ordering is worse than no indentation: a child
    rendered five rows below an unrelated folder reads as belonging to it. One
    query, then the tree is assembled in Python — the depth cap is 3, so the
    recursion is bounded by construction.
    """
    folders = list(MediaFolder.objects.for_workspace(workspace))
    children: dict[Any, list[MediaFolder]] = {}
    for folder in folders:
        children.setdefault(folder.parent_id, []).append(folder)

    rows: list[dict[str, Any]] = []

    def walk(parent_id: Any, depth: int) -> None:
        for folder in sorted(children.get(parent_id, []), key=lambda f: f.name.lower()):
            rows.append({"id": str(folder.pk), "name": folder.name, "indent": depth + 0.625})
            walk(folder.pk, depth + 1)

    walk(None, 0)
    return rows


def _can_manage(request: WorkspaceRequest) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("manage_media", False))


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
#
# One context builder per fragment. They used to be a single function that built
# the whole page for every render, so a grid refresh ran the folder query and a
# SUM aggregate it never displayed, and a folder-rail refresh paginated 48 assets
# it never displayed — twice over, because one `mediaChanged` event refreshes
# both regions.


def _grid_context(request: WorkspaceRequest, params: Any = None) -> dict[str, Any]:
    """What ``_asset_grid.html`` draws.

    ``params`` defaults to ``request.GET`` but is passed explicitly by ``upload``,
    which is a POST: reading ``request.GET`` there would find nothing and hand
    back an unfiltered page-one grid, discarding whatever folder, kind or search
    the user was looking at when they dropped the files.
    """
    from django.core.paginator import Paginator

    params = request.GET if params is None else params
    kind = params.get("kind", "")
    folder = params.get("folder", "")
    term = params.get("q", "").strip()

    assets = filter_assets(request.workspace, kind=kind, folder=folder, term=term)
    return {
        "page": Paginator(assets.select_related("uploaded_by"), GRID_PAGE_SIZE).get_page(params.get("page", 1)),
        "query": term,
        "current_kind": kind,
        "current_folder": folder,
    }


def _folders_context(request: WorkspaceRequest) -> dict[str, Any]:
    """What ``_folder_rail.html`` draws."""
    return {
        "folder_rows": _folder_rows(request.workspace),
        "current_folder": request.GET.get("folder", ""),
        "can_manage": _can_manage(request),
    }


def _library_context(request: WorkspaceRequest) -> dict[str, Any]:
    """The full page: both fragments plus the chrome around them."""
    return {
        **_grid_context(request),
        **_folders_context(request),
        "kinds": MediaKind.choices,
        "accepted_types": accepted_upload_types(),
        "used_bytes": used_bytes(request.workspace),
        "quota_bytes": workspace_quota_bytes(),
    }


@login_required
@require_GET
def library(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    if _is_htmx(request):
        # Two fragments, one endpoint. The folder rail sits outside #media-grid,
        # so the `mediaChanged` event that refreshes the grid would otherwise
        # leave a newly created folder invisible until a full page load.
        if request.GET.get("fragment") == "folders":
            return render(request, "media_library/_folder_rail.html", _folders_context(request))
        return render(request, "media_library/_asset_grid.html", _grid_context(request))
    return render(request, "media_library/library.html", _library_context(request))


@login_required
@require_GET
def asset_detail(request: WorkspaceRequest, workspace_id: str, asset_id: str) -> HttpResponse:
    asset = get_scoped_object_or_404(MediaAsset, request.workspace, pk=asset_id)
    context = {
        "asset": asset,
        "folders": MediaFolder.objects.for_workspace(request.workspace),
        "can_manage": _can_manage(request),
    }
    return render(request, "media_library/_asset_detail.html", context)


@login_required
@require_GET
def picker(request: WorkspaceRequest, workspace_id: str) -> JsonResponse:
    """The JSON contract documented in :mod:`apps.media_library.picker`."""
    return JsonResponse(
        picker_payload(
            workspace=request.workspace,
            term=request.GET.get("q", "").strip(),
            kind=request.GET.get("kind", ""),
            folder=request.GET.get("folder", ""),
            platform=request.GET.get("platform", ""),
            cursor=request.GET.get("cursor", ""),
            limit=_int_param(request.GET.get("limit"), DEFAULT_LIMIT),
        )
    )


def _int_param(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_media")
@require_POST
def upload(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Accept a batch of files, one validation verdict each.

    A batch is not all-or-nothing: one oversized file among ten should not
    discard the nine that were fine, so each is reported separately and the
    caller is told how many landed.
    """
    from django.conf import settings

    files = request.FILES.getlist("files")
    if not files:
        return JsonResponse({"error": "No files were sent."}, status=400)

    max_files = int(settings.MEDIA_MAX_FILES_PER_UPLOAD)
    if len(files) > max_files:
        return JsonResponse({"error": f"Up to {max_files} files per upload."}, status=400)

    folder = _folder_or_404(request, request.POST.get("folder"))

    created: list[MediaAsset] = []
    errors: list[dict[str, str]] = []
    for uploaded_file in files:
        try:
            created.append(
                services.create_asset(
                    workspace=request.workspace,
                    uploaded_file=uploaded_file,
                    uploaded_by=request.user,
                    folder=folder,
                )
            )
        except (UnsupportedMediaError, QuotaExceededError) as exc:
            errors.append({"filename": uploaded_file.name or "file", "error": str(exc)})

    if _is_htmx(request):
        response = render(request, "media_library/_asset_grid.html", _grid_context(request, request.POST))
        tone = "warn" if errors else "success"
        title = f"{len(created)} uploaded" if created else "Nothing uploaded"
        body = "; ".join(f"{e['filename']}: {e['error']}" for e in errors[:3])
        return _with_trigger(response, _toast(tone, title, body))

    status = 200 if created else 400
    return JsonResponse({"uploaded": [str(a.pk) for a in created], "errors": errors}, status=status)


# ---------------------------------------------------------------------------
# Asset mutations
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_media")
@require_POST
def asset_edit(request: WorkspaceRequest, workspace_id: str, asset_id: str) -> HttpResponse:
    asset = get_scoped_object_or_404(MediaAsset, request.workspace, pk=asset_id)
    # No "" default: update_asset treats None as "leave this alone", and passing
    # "" for a field the caller never sent would silently erase it.
    services.update_asset(
        asset,
        title=request.POST.get("title"),
        alt_text=request.POST.get("alt_text"),
    )
    return toast_response(tone="success", title="Saved", events={"mediaChanged": True})


@login_required
@require_permission("manage_media")
@require_POST
def asset_move(request: WorkspaceRequest, workspace_id: str, asset_id: str) -> HttpResponse:
    asset = get_scoped_object_or_404(MediaAsset, request.workspace, pk=asset_id)
    folder = _folder_or_404(request, request.POST.get("folder"))
    services.move_asset(asset, folder)
    return toast_response(
        tone="success",
        title="Moved" if folder else "Moved to the library root",
        body=folder.name if folder else "",
        events={"mediaChanged": True},
    )


@login_required
@require_permission("manage_media")
@require_POST
def asset_delete(request: WorkspaceRequest, workspace_id: str, asset_id: str) -> HttpResponse:
    asset = get_scoped_object_or_404(MediaAsset, request.workspace, pk=asset_id)
    filename = asset.filename
    services.delete_asset(asset)
    return toast_response(
        tone="success",
        title="Deleted",
        body=f"{filename} is gone, and every delivery URL for it now fails.",
        events={"mediaChanged": True},
    )


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_media")
@require_POST
def folder_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    name = request.POST.get("name", "").strip()
    if not name:
        return _rejected("A folder needs a name.")
    parent = _folder_or_404(request, request.POST.get("parent"))
    try:
        services.create_folder(workspace=request.workspace, name=name, parent=parent)
    except ValidationError as exc:
        return _rejected(_first_message(exc))
    return toast_response(tone="success", title="Folder created", events={"mediaChanged": True})


@login_required
@require_permission("manage_media")
@require_POST
def folder_rename(request: WorkspaceRequest, workspace_id: str, folder_id: str) -> HttpResponse:
    folder = get_scoped_object_or_404(MediaFolder, request.workspace, pk=folder_id)
    name = request.POST.get("name", "").strip()
    if not name:
        return _rejected("A folder needs a name.")
    try:
        services.rename_folder(folder, name)
    except ValidationError as exc:
        return _rejected(_first_message(exc))
    return toast_response(tone="success", title="Folder renamed", events={"mediaChanged": True})


@login_required
@require_permission("manage_media")
@require_POST
def folder_delete(request: WorkspaceRequest, workspace_id: str, folder_id: str) -> HttpResponse:
    folder = get_scoped_object_or_404(MediaFolder, request.workspace, pk=folder_id)
    parent_id = folder.parent_id
    try:
        services.delete_folder(folder)
    except ValidationError as exc:
        return _rejected(_first_message(exc))

    # Deleting the folder you are looking at leaves a filter pointing at an id
    # that no longer resolves: the grid refetches with it and gets the 404 that
    # every unknown folder id gets, and the upload and new-folder forms keep
    # posting it because they sit outside the region the refresh replaces.
    # Redirect instead — the contents moved to the parent, so that is where the
    # user should land, and a full navigation clears every copy of the stale id
    # at once.
    if request.POST.get("current_folder") == str(folder_id):
        destination = reverse("media:library", kwargs={"workspace_id": workspace_id})
        if parent_id:
            destination = f"{destination}?folder={parent_id}"
        return HttpResponse(status=204, headers={"HX-Redirect": destination})

    return toast_response(
        tone="success",
        title="Folder deleted",
        body="Anything inside it moved up one level.",
        events={"mediaChanged": True},
    )


def _first_message(exc: ValidationError) -> str:
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else str(exc)


def _rejected(message: str) -> HttpResponse:
    """A validation failure the client can actually detect.

    These used to answer 204 with an error-toned toast, which reads fine in a
    screenshot and is wrong in every other way: htmx sets ``detail.successful``
    from the status, so 204 made a rejected folder name indistinguishable from
    an accepted one — the new-folder form's ``if (event.detail.successful)``
    guard cleared the field on failure, and no non-browser caller could branch
    at all.

    400 with a short plain-text body is the shape the shell already handles:
    templates/partials/_toast_host.html listens for ``htmx:responseError`` and
    renders a body under 300 characters as the error toast, so the message still
    reaches the user without a second convention.
    """
    return HttpResponse(message, status=400, content_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Public delivery — no session, the token is the credential
# ---------------------------------------------------------------------------


@require_GET
def deliver(request: HttpRequest, token: str) -> HttpResponseBase:
    """Serve one asset to whoever holds a valid token.

    Unauthenticated by design: the fetcher is a messaging platform, not a
    browser with a session. Every rejection — bad signature, wrong purpose,
    unknown version, deleted asset — is the same bare 404.
    """
    asset_id, thumbnail = read_token(token)

    # .unscoped() is correct and deliberate here: there is no session and no
    # workspace on this path, and the signed token is what authorises the read.
    # It is the only unscoped query in this app.
    asset = MediaAsset.objects.unscoped().filter(pk=asset_id).first()
    if asset is None:
        raise Http404

    return delivery_response(asset, thumbnail=thumbnail)
