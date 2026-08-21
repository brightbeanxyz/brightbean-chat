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
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.common.shortcuts import get_scoped_object_or_404
from apps.media_library import services
from apps.media_library.delivery import delivery_response, delivery_url, read_token
from apps.media_library.mimes import MediaKind, UnsupportedMediaError, accepted_upload_types
from apps.media_library.models import MediaAsset, MediaFolder
from apps.media_library.picker import DEFAULT_LIMIT, ROOT_FOLDER, picker_payload, search
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


def _visible_assets(request: WorkspaceRequest) -> Any:
    """The grid queryset for the current filters."""
    assets: QuerySet = MediaAsset.objects.for_workspace(request.workspace).select_related("folder", "uploaded_by")

    kind = request.GET.get("kind", "")
    if kind in MediaKind.values:
        assets = assets.filter(kind=kind)

    folder = request.GET.get("folder", "")
    if folder == ROOT_FOLDER:
        assets = assets.filter(folder__isnull=True)
    elif folder:
        assets = assets.filter(folder=_folder_or_404(request, folder))

    term = request.GET.get("q", "").strip()
    if term:
        assets = search(assets, term)

    return assets.order_by("-created_at", "-id")


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------


def _library_context(request: WorkspaceRequest) -> dict[str, Any]:
    """Everything both the full page and the grid partial need.

    A function rather than a view calling a view: ``library`` is decorated
    ``@require_GET``, so ``upload`` re-entering it to re-render the grid after a
    POST answered 405 — a 405 with an empty body, which htmx does not swap, so
    the upload succeeded and the page silently showed nothing.
    """
    from django.core.paginator import Paginator

    return {
        "page": Paginator(_visible_assets(request), GRID_PAGE_SIZE).get_page(request.GET.get("page", 1)),
        "folder_rows": _folder_rows(request.workspace),
        "query": request.GET.get("q", "").strip(),
        "current_kind": request.GET.get("kind", ""),
        "current_folder": request.GET.get("folder", ""),
        "kinds": MediaKind.choices,
        "accepted_types": accepted_upload_types(),
        "can_manage": request.workspace_membership.effective_permissions.get("manage_media", False),
        "used_bytes": used_bytes(request.workspace),
        "quota_bytes": workspace_quota_bytes(),
    }


@login_required
@require_GET
def library(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    context = _library_context(request)
    if _is_htmx(request):
        # Two fragments, one endpoint. The folder rail sits outside #media-grid,
        # so the `mediaChanged` event that refreshes the grid would otherwise
        # leave a newly created folder invisible until a full page load.
        if request.GET.get("fragment") == "folders":
            return render(request, "media_library/_folder_rail.html", context)
        return render(request, "media_library/_asset_grid.html", context)
    return render(request, "media_library/library.html", context)


@login_required
@require_GET
def asset_detail(request: WorkspaceRequest, workspace_id: str, asset_id: str) -> HttpResponse:
    asset = get_scoped_object_or_404(MediaAsset, request.workspace, pk=asset_id)
    context = {
        "asset": asset,
        "folders": MediaFolder.objects.for_workspace(request.workspace),
        "delivery_url": delivery_url(asset),
        "can_manage": request.workspace_membership.effective_permissions.get("manage_media", False),
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
        response = render(request, "media_library/_asset_grid.html", _library_context(request))
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
    services.update_asset(
        asset,
        title=request.POST.get("title", ""),
        alt_text=request.POST.get("alt_text", ""),
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
        return toast_response(tone="error", title="A folder needs a name")
    parent = _folder_or_404(request, request.POST.get("parent"))
    try:
        services.create_folder(workspace=request.workspace, name=name, parent=parent)
    except ValidationError as exc:
        return toast_response(tone="error", title="Could not create that folder", body=_first_message(exc))
    return toast_response(tone="success", title="Folder created", events={"mediaChanged": True})


@login_required
@require_permission("manage_media")
@require_POST
def folder_rename(request: WorkspaceRequest, workspace_id: str, folder_id: str) -> HttpResponse:
    folder = get_scoped_object_or_404(MediaFolder, request.workspace, pk=folder_id)
    name = request.POST.get("name", "").strip()
    if not name:
        return toast_response(tone="error", title="A folder needs a name")
    try:
        services.rename_folder(folder, name)
    except ValidationError as exc:
        return toast_response(tone="error", title="Could not rename that folder", body=_first_message(exc))
    return toast_response(tone="success", title="Folder renamed", events={"mediaChanged": True})


@login_required
@require_permission("manage_media")
@require_POST
def folder_delete(request: WorkspaceRequest, workspace_id: str, folder_id: str) -> HttpResponse:
    folder = get_scoped_object_or_404(MediaFolder, request.workspace, pk=folder_id)
    services.delete_folder(folder)
    return toast_response(
        tone="success",
        title="Folder deleted",
        body="Anything inside it moved up one level.",
        events={"mediaChanged": True},
    )


def _first_message(exc: ValidationError) -> str:
    messages = getattr(exc, "messages", None)
    return messages[0] if messages else str(exc)


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
