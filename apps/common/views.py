"""Views owned by ``apps.common``: the health probe, the UI style guide, and
the "coming soon" stubs behind the sidebar navigation.

There is no landing page here. ``/`` is ``apps.accounts.views.root``, which
sends anonymous visitors to the login page — the shell's own placeholder
existed only while there was no way to log in.
"""

import logging
from collections.abc import Callable
from typing import Any, cast

from django.contrib.auth.decorators import login_required
from django.db import Error as DatabaseError
from django.db import connections
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.common.context_processors import navigation_context
from apps.common.htmx import Tone, toast_response
from apps.members.decorators import require_permission

logger = logging.getLogger(__name__)


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe with a real database round-trip (SPEC §20).

    Studio's ``/health/`` returns a bare JSON ``ok`` and never touches the
    database, so it stays green while the app is entirely unable to serve
    traffic. This one actually asks Postgres a question.

    The failure body carries no exception detail: the endpoint is reachable
    unauthenticated (it is exempt from the production HTTPS redirect so
    in-cluster probes can hit it), and DB errors leak connection strings,
    hostnames and schema names.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except (DatabaseError, OSError) as exc:
        logger.error("Health check failed: database unreachable (%s)", type(exc).__name__)
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"})


def ui_demo(request: HttpRequest) -> HttpResponse:
    """The design system's living style guide.

    Exercises the three things a later UI issue will reach for first and that
    are otherwise only provable by hand: a toast fired from a real
    ``toast_response`` view with no per-page include, ``{% ui_select %}``
    rendering outside the one page Studio's version worked on, and every
    platform icon including the unknown-key fallback.

    It renders the app chrome regardless of authentication. There is no login
    page until issue #31 merges, and a design system nobody can look at is a
    design system nobody reviews. Nothing here reads a database or a session —
    it is static markup plus the context processor's model-free navigation.
    """
    context = navigation_context(request)
    context["ui_demo_platforms"] = ["telegram", "instagram", "messenger", "whatsapp", "sms", "email", "carrier-pigeon"]
    context["ui_demo_statuses"] = [("open", "Open"), ("snoozed", "Snoozed"), ("closed", "Closed")]
    context["ui_demo_channels"] = [
        {"value": "tg-main", "label": "Support bot", "icon": "telegram"},
        {"value": "ig-shop", "label": "Shop DMs", "icon": "instagram"},
        {"value": "wa-eu", "label": "EU WhatsApp", "icon": "whatsapp"},
    ]
    return render(request, "ui_demo.html", context)


@require_POST
def ui_demo_toast(request: HttpRequest) -> HttpResponse:
    """Fire a toast of the requested tone, to prove the host needs no include."""
    bodies: dict[Tone, str] = {
        "success": "Nothing was saved — this is the style guide.",
        "info": "Toasts arrive over HX-Trigger, rendered by the host in base.html.",
        "warn": "The body is written with textContent, so markup here is inert.",
        "error": "Errors stay visible longer and use the alert role.",
    }
    # One table, not two. The keys of `bodies` already are the valid tones, so a
    # second dict listing them again is a place for the two to drift — add a
    # fifth tone, forget one, and it silently degrades to "info". The cast is
    # what the membership test has just proved.
    raw = request.POST.get("tone", "")
    tone: Tone = cast(Tone, raw) if raw in bodies else "info"
    return toast_response(tone=tone, title=f"{tone.title()} toast", body=bodies[tone])


# The shells a stub may render into, keyed by a symbolic name. A dict rather
# than a template path passed straight through: `layout` reaches
# {% extends %}, and a template name that can be influenced by request data is
# how a URLconf change one refactor from now turns into arbitrary template
# disclosure. SECURITY-BASELINE §3 bans user input reaching the template
# engine; this keeps the ban true by construction rather than by review.
_LAYOUTS = {
    "app": "base.html",
    "settings": "layouts/settings.html",
    "workspace_settings": "layouts/workspace_settings.html",
}


def coming_soon(
    request: HttpRequest,
    *,
    section: str,
    issue: str,
    layout: str = "app",
    workspace_id: str | None = None,
) -> HttpResponse:
    """Placeholder for a sidebar destination a later issue owns.

    The navigation is complete from day one so the shell can be reviewed and so
    later issues change a view, not the nav. Each stub names the issue that
    replaces it.

    ``layout`` is a key of :data:`_LAYOUTS`, not a template path, so the
    settings routes exercise ``layouts/settings.html`` and
    ``layouts/workspace_settings.html`` without the template name ever being a
    function of request data. An unknown key falls back to the app shell.

    ``workspace_id`` is accepted but unused: the workspace-scoped stubs live
    under ``/w/<uuid:workspace_id>/`` so they land where their real views will,
    and RBACMiddleware resolves the workspace from that kwarg before this runs.
    """
    context = {"section": section, "issue": issue, "layout": _LAYOUTS.get(layout, _LAYOUTS["app"])}
    return render(request, "coming_soon.html", context)


def workspace_stub(permission: str) -> Callable[..., HttpResponse]:
    """A ``coming_soon`` guarded by a workspace permission.

    Every workspace-scoped placeholder is a real endpoint under
    ``/w/<uuid:workspace_id>/``, so it is bound by SECURITY-BASELINE §1 like
    any other: a member of a different workspace must get a 404, never a 403,
    and the IDOR sweep in ``tests/idor.py`` walks it automatically. Gating on
    the permission the real view will use means the placeholder and its
    replacement refuse the same people.
    """

    @login_required
    @require_permission(permission)
    def view(request: HttpRequest, workspace_id: str, **kwargs: Any) -> HttpResponse:
        return coming_soon(request, workspace_id=workspace_id, **kwargs)

    return view


def account_stub(request: HttpRequest, **kwargs: Any) -> HttpResponse:
    """A ``coming_soon`` for a route that is not workspace-scoped."""
    return login_required(lambda r: coming_soon(r, **kwargs))(request)
