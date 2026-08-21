"""Views owned by the shell: the landing page, the UI style guide, the health
probe, and the "coming soon" stubs behind the sidebar navigation."""

import logging

from django.db import Error as DatabaseError
from django.db import connections
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.common.context_processors import navigation_context
from apps.common.htmx import Tone, toast_response

logger = logging.getLogger(__name__)


def index(request: HttpRequest) -> HttpResponse:
    """The landing page, and the app shell once there is a session.

    ``base.html`` branches on ``show_app_shell``: an authenticated visitor gets
    the sidebar shell, everyone else gets the centred ``auth_content`` layout.
    This route stays reachable unauthenticated and returns 200 — CI boots the
    compose stack from a checkout with no ``.env`` and asserts exactly that.
    """
    return render(request, "index.html")


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
    # A dict keyed by the literal type rather than an `in` check, so mypy can
    # see that whatever comes out of untrusted POST data really is a Tone.
    bodies: dict[Tone, str] = {
        "success": "Nothing was saved — this is the style guide.",
        "info": "Toasts arrive over HX-Trigger, rendered by the host in base.html.",
        "warn": "The body is written with textContent, so markup here is inert.",
        "error": "Errors stay visible longer and use the alert role.",
    }
    tones: dict[str, Tone] = {"success": "success", "info": "info", "warn": "warn", "error": "error"}
    tone: Tone = tones.get(request.POST.get("tone", ""), "info")
    return toast_response(tone=tone, title=f"{tone.title()} toast", body=bodies[tone])


def coming_soon(request: HttpRequest, *, section: str, issue: str, layout: str = "base.html") -> HttpResponse:
    """Placeholder for a sidebar destination a later issue owns.

    The navigation is complete from day one so the shell can be reviewed and so
    later issues change a view, not the nav. Each stub names the issue that
    replaces it.

    ``layout`` picks the shell the stub renders into, so the settings routes
    exercise ``layouts/settings.html`` and ``layouts/workspace_settings.html``
    rather than leaving both layouts unrendered by anything until Layer 2.
    """
    return render(request, "coming_soon.html", {"section": section, "issue": issue, "layout": layout})
