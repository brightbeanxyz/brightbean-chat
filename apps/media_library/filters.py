"""Turning ``?kind=``/``?folder=``/``?q=`` into a scoped queryset, once.

The library grid and the picker endpoint offer the same three filters over the
same tenant-scoped queryset, and they used to implement them separately. They
had already drifted: a folder id belonging to another workspace answered 404 on
the grid and an empty result set in the picker, which is two different stories
about the same request. The next filter either app grows would have been written
twice too.

Resolution is 404 for anything that does not name a folder in this workspace —
malformed, deleted, or someone else's, all indistinguishable, which is the rule
every other id in this app follows (SECURITY-BASELINE §1). Silence would be
worse for the picker's consumers than an error: a stale folder id cached in a
flow builder should say so rather than render an empty library.
"""

from typing import Any
from uuid import UUID

from django.db.models import Q, QuerySet
from django.http import Http404

from apps.media_library.mimes import MediaKind
from apps.media_library.models import MediaAsset, MediaFolder

__all__ = ["ROOT_FOLDER", "filter_assets", "resolve_folder", "search"]

#: Sentinel for "assets in no folder at all". A caller cannot express that with
#: an id, and an absent ``folder`` already means "do not filter".
ROOT_FOLDER = "root"


def resolve_folder(workspace: Any, folder: str) -> tuple[MediaFolder | None, bool]:
    """``(folder, root_only)`` for one ``?folder=`` value.

    ``("", False)`` means no folder filter at all; ``(None, True)`` means the
    library root. Raises ``Http404`` for an id this workspace cannot see.
    """
    if not folder:
        return None, False
    if folder == ROOT_FOLDER:
        return None, True

    try:
        pk = UUID(folder)
    except (ValueError, AttributeError, TypeError) as exc:
        raise Http404("No such folder.") from exc

    # Scoped, so another tenant's id is a miss rather than a cross-tenant read.
    resolved = MediaFolder.objects.for_workspace(workspace).filter(pk=pk).first()
    if resolved is None:
        raise Http404("No such folder.")
    return resolved, False


def search(queryset: QuerySet, term: str) -> QuerySet:
    """Case-insensitive match across the three human-authored fields.

    ORM ``icontains`` rather than Studio's ``SearchVector``/``SearchRank``: full
    text search buys stemming and ranking that a library of a few thousand
    filenames does not need, and it needs an index and a migration to not be
    slow. Plain matching is predictable, and — the part that matters for
    SECURITY-BASELINE §7 — it compiles entirely through the ORM with no
    string-built SQL and no user-controlled field names.
    """
    return queryset.filter(Q(filename__icontains=term) | Q(title__icontains=term) | Q(alt_text__icontains=term))


def filter_assets(
    workspace: Any,
    *,
    kind: str = "",
    folder: str = "",
    term: str = "",
) -> QuerySet:
    """The scoped, filtered, ordered asset queryset both surfaces render."""
    resolved, root_only = resolve_folder(workspace, folder)

    assets: QuerySet = MediaAsset.objects.for_workspace(workspace).select_related("folder")
    if kind in MediaKind.values:
        assets = assets.filter(kind=kind)
    if root_only:
        assets = assets.filter(folder__isnull=True)
    elif resolved is not None:
        assets = assets.filter(folder=resolved)
    if term:
        assets = search(assets, term)

    # (created_at, id) is the model's own ordering and a stable one, which is
    # what the picker's keyset pagination needs to be correct.
    return assets.order_by("-created_at", "-id")
