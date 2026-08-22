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

**Write views do not render a surface.** Two surfaces show the same rows — the
bell dropdown and the history page — and an earlier cut had ``mark_read``
return the bell partial to whoever asked, so marking a row read from the
history page swapped the dropdown into the list container and lost the reader's
filter and page. Guessing from the request was the wrong fix; the views now
return only the out-of-band badge plus an ``HX-Trigger``, and each surface
re-fetches *itself* with its own state. Adding a third surface needs no change
here.
"""

import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.htmx import toast_response
from apps.members.requests import RBACRequest
from apps.notifications import action_urls, selectors
from apps.notifications.events import REGISTRY, registered_choices
from apps.notifications.models import Notification, NotificationSetting

#: Rows per page on the history view.
PAGE_SIZE = 30

#: Ceiling on the page number a request may ask for. Far past any real feed;
#: it exists to keep the OFFSET inside a bigint.
MAX_PAGE = 100_000


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
    }


#: Fired after anything is marked read. Every surface showing notifications
#: listens for it and re-fetches itself, carrying whatever filter or page state
#: it happens to hold — which is state the write view does not have and should
#: not try to reconstruct.
CHANGED_EVENT = "notificationsChanged"


def _changed_response(request: Any) -> HttpResponse:
    """The badge, plus the event that tells each surface to refresh.

    Not ``apps.common.htmx.trigger_response``: that returns a bodyless 204 by
    design, and this response needs a body — the out-of-band badge fragment —
    so the count updates in the same round trip that fires the event.
    """
    response = render(
        request,
        "notifications/partials/_badge.html",
        {"unread_notification_count": selectors.unread_count_for(request.user)},
    )
    response["HX-Trigger"] = json.dumps({CHANGED_EVENT: True})
    return response


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
        # Clamped, not just floored: Python ints are arbitrary precision, so
        # ?page=99999999999999999999 parses fine and then overflows the bigint
        # Postgres wants for OFFSET, turning a nonsense page number into a 500
        # rather than the empty page the except clause was written to produce.
        page = min(MAX_PAGE, max(1, int(request.GET.get("page", 1))))
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

    destination = _followed_destination(request, notification)

    if _is_htmx(request):
        response = _changed_response(request)
        if destination:
            # htmx swallows the anchor's own navigation once hx-post is on it,
            # so without this the reader clicks a notification, watches it turn
            # read, and has to click again to actually get where it pointed.
            # The row asks for this explicitly (hx-vals follow=1) rather than it
            # being implied by having an action_url, because the history page's
            # "Mark read" button posts the same route and must stay put.
            response["HX-Redirect"] = destination
        return response
    return redirect(destination or "notifications:list")


def _followed_destination(request: Any, notification: Notification) -> str | None:
    """Where this click should land, if it asked to be followed.

    The stored path was already reduced to this origin at write time
    (:mod:`apps.notifications.action_urls`), and it is re-checked here because
    a redirect built from a stored value is exactly where a stale row would
    matter.
    """
    if request.POST.get("follow") != "1":
        return None
    payload = notification.payload if isinstance(notification.payload, dict) else {}
    return action_urls.safe_path(payload.get("action_url"))


@login_required
@require_POST
def mark_all_read(request: RBACRequest) -> HttpResponse:
    """Mark everything read in one statement.

    ``update()`` bypasses ``auto_now``, so ``updated_at`` is set explicitly —
    otherwise the bulk path would leave it at creation time while the
    single-row path above moves it, and the column would mean two things.
    """
    now = timezone.now()
    selectors.feed_for(request.user).filter(is_read=False).update(is_read=True, read_at=now, updated_at=now)

    if _is_htmx(request):
        return _changed_response(request)
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
