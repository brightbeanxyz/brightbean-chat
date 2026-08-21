"""The bell, the history page, and the one preference.

Every view is gated on ``@login_required`` and nothing else. There is no
workspace permission key for notifications — the Layer-2 table says so
explicitly ("per-user, not workspace-gated") — because the row already names
the only person entitled to it.

That makes ``apps.common.shortcuts.get_scoped_object_or_404`` the **wrong**
helper here, which is worth saying out loud since it is the house default:
scoping a notification to ``request.workspace`` would let a colleague in the
same workspace mark someone else's notification read. The boundary is the user,
so the lookup is ``get_object_or_404(..., user=request.user)`` — still a 404,
never a 403, so a probe cannot distinguish "not yours" from "no such id".
"""

from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.members.requests import RBACRequest
from apps.notifications import selectors
from apps.notifications.events import REGISTRY, registered_choices
from apps.notifications.models import Notification, NotificationSetting

#: Rows per page on the history view.
PAGE_SIZE = 30


def _is_htmx(request: Any) -> bool:
    """django-htmx is not a dependency here, so there is no ``request.htmx``.

    Studio branches on that attribute in every notification view; porting those
    branches verbatim would have silently taken the full-page path forever,
    because a missing attribute is falsy in exactly the way that hides.
    """
    return request.headers.get("HX-Request") == "true"


def _email_enabled(user: Any) -> bool:
    """Absence of a row means enabled — the read path never writes one."""
    setting = NotificationSetting.objects.filter(user=user).first()
    return True if setting is None else setting.email_enabled


def _bell_context(request: Any) -> dict[str, Any]:
    return {
        "notifications": selectors.recent_for(request.user),
        "unread_notification_count": selectors.unread_count_for(request.user),
        "registry": REGISTRY,
    }


@login_required
@require_GET
def notification_list(request: RBACRequest) -> HttpResponse:
    """The full history, with type and read-state filters."""
    queryset = selectors.feed_for(request.user)

    event_type = request.GET.get("event_type", "")
    read_state = request.GET.get("read_state", "")
    if event_type in REGISTRY:
        queryset = queryset.filter(event_type=event_type)
    if read_state == "unread":
        queryset = queryset.filter(is_read=False)
    elif read_state == "read":
        queryset = queryset.filter(is_read=True)

    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    offset = (page - 1) * PAGE_SIZE
    # One extra row rather than a second count() query: its presence is the
    # answer to "is there a next page".
    window = list(queryset[offset : offset + PAGE_SIZE + 1])

    context = {
        "notifications": window[:PAGE_SIZE],
        "event_type_choices": registered_choices(),
        "selected_event_type": event_type,
        "selected_read_state": read_state,
        "page": page,
        "has_next": len(window) > PAGE_SIZE,
        "has_previous": page > 1,
        "registry": REGISTRY,
        "email_enabled": _email_enabled(request.user),
        "unread_notification_count": selectors.unread_count_for(request.user),
    }
    if _is_htmx(request):
        return render(request, "notifications/partials/_history_list.html", context)
    return render(request, "notifications/list.html", context)


@login_required
@require_GET
def bell_panel(request: RBACRequest) -> HttpResponse:
    """The dropdown's contents. Re-fetched on every open, so it is never stale."""
    return render(request, "notifications/partials/_bell_panel.html", _bell_context(request))


@login_required
@require_GET
def unread_badge(request: RBACRequest) -> HttpResponse:
    """The count, as HTML rather than Studio's JSON — htmx swaps markup."""
    return render(
        request,
        "notifications/partials/_badge.html",
        {"unread_notification_count": selectors.unread_count_for(request.user)},
    )


@login_required
@require_POST
def mark_read(request: RBACRequest, notification_id: Any) -> HttpResponse:
    """Mark one notification read.

    ``user=request.user`` in the lookup is the whole authorization check, and a
    miss is a 404 — indistinguishable from an id that names nothing.
    """
    notification = get_object_or_404(Notification, pk=notification_id, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at", "updated_at"])

    if _is_htmx(request):
        return render(request, "notifications/partials/_bell_panel.html", _bell_context(request))
    return redirect("notifications:list")


@login_required
@require_POST
def mark_all_read(request: RBACRequest) -> HttpResponse:
    """Mark everything read in one statement."""
    selectors.feed_for(request.user).filter(is_read=False).update(is_read=True, read_at=timezone.now())

    if _is_htmx(request):
        return render(request, "notifications/partials/_bell_panel.html", _bell_context(request))
    return redirect("notifications:list")


@login_required
@require_POST
def update_email_preference(request: RBACRequest) -> HttpResponse:
    """The single preference the issue allows: email me, or do not."""
    enabled = request.POST.get("email_enabled") == "on"
    NotificationSetting.objects.update_or_create(user=request.user, defaults={"email_enabled": enabled})

    if _is_htmx(request):
        return toast_response(
            tone="success",
            title="Preferences saved",
            body="Notification emails are now " + ("on." if enabled else "off."),
        )
    return redirect("notifications:list")
