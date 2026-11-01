"""Account-level views: the root router and the post-login landing logic."""

from typing import Any

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.accounts.services import ensure_provisioned
from apps.common import themes
from apps.members.models import WorkspaceMembership
from apps.members.requests import RBACRequest


def resolve_landing_workspace_id(user: Any) -> Any | None:
    """Where this user should land.

    ``last_workspace_id`` when it still names a live workspace they belong to,
    otherwise their first non-archived membership. Both paths filter
    ``is_archived`` — an archived workspace is not somewhere to land, and
    ``RBACMiddleware`` would 404 the redirect anyway.
    """
    memberships = WorkspaceMembership.objects.filter(user=user, workspace__is_archived=False).select_related(
        "workspace"
    )

    last_workspace_id = getattr(user, "last_workspace_id", None)
    if last_workspace_id and memberships.filter(workspace_id=last_workspace_id).exists():
        return last_workspace_id

    membership = memberships.order_by("workspace__name").first()
    return membership.workspace_id if membership else None


def root(request: HttpRequest) -> HttpResponse:
    """``/`` — send people where they are supposed to be.

    Also the provisioning safety net for accounts created outside signup
    (``createsuperuser``, the admin, a shell): see
    ``apps.accounts.services.ensure_provisioned``.
    """
    if not request.user.is_authenticated:
        return redirect(reverse("account_login"))

    ensure_provisioned(request.user)

    workspace_id = resolve_landing_workspace_id(request.user)
    if workspace_id is None:
        # Every workspace archived. The org-level list is the only place they
        # can be brought back, so send them there rather than to a dead end.
        return redirect(reverse("organizations:workspaces"))

    return redirect(reverse("workspaces:dashboard", kwargs={"workspace_id": workspace_id}))


@login_required
def account_settings(request: RBACRequest) -> HttpResponse:
    """Minimal profile page: display name only.

    Password and email management are allauth's own routes; this exists so the
    settings navigation has an Account section to point at.
    """
    if request.method == "POST":
        request.user.name = (request.POST.get("name") or "").strip()[:255]
        request.user.save(update_fields=["name"])
        return redirect(reverse("accounts:settings"))
    return render(request, "accounts/settings.html")


@login_required
def account_preferences(request: RBACRequest) -> HttpResponse:
    """Language, theme and colour mode.

    Replaces config/urls.py's old ``settings_preferences`` stub. Each choice is
    saved on the user and nothing more is needed: the redirect below is a fresh
    request, and both readers take the field straight off it —
    ``apps.accounts.middleware.LanguagePreferenceMiddleware`` for the language,
    ``{% theme_attrs %}`` in base.html for theme and mode.

    A field is only written when the form actually posted it. The theme and
    mode selects are left out of the page while they offer no choice (see
    below), and reading a missing key as "" would reset them to the default
    every time someone saved their language.
    """
    if request.method == "POST":
        allowed = {
            "language": {code for code, _ in settings.LANGUAGES},
            "theme": set(themes.THEMES),
            # Every mode, not only the current theme's: an unsupported one is
            # kept and clamped at render time by themes.resolve.
            "color_mode": set(themes.COLOR_MODES),
        }
        changed = []
        for field, valid in allowed.items():
            if field not in request.POST:
                continue
            value = request.POST[field].strip()
            if value in valid or value == "":
                setattr(request.user, field, value)
                changed.append(field)
        if changed:
            request.user.save(update_fields=changed)
        return redirect(reverse("settings_preferences"))

    # A select is only drawn when it is a real choice. With one theme that has
    # only a light palette (Phase 1 of docs/themes-roadmap.md) neither is, and
    # the page stays the language picker it was; the mode select appears by
    # itself once the theme's registry entry lists a second mode.
    theme, _mode = themes.resolve(request.user.theme, request.user.color_mode)
    context: dict[str, Any] = {
        "theme_choices": [(t.slug, t.label) for t in themes.THEMES.values()] if len(themes.THEMES) > 1 else [],
        "mode_choices": [(m, themes.COLOR_MODES[m]) for m in theme.modes] if len(theme.modes) > 1 else [],
    }
    return render(request, "accounts/preferences.html", context)
