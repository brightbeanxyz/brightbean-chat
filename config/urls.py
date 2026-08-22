from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import URLPattern, include, path

from apps.accounts import views as account_views
from apps.common import views

# Sidebar destinations that no issue has built yet. Every one names the issue
# that replaces it, and the nav entry does not change when that happens — the
# owning issue swaps the view, not apps.common.context_processors.
#
# Workspace-scoped stubs sit under /w/<uuid:workspace_id>/ (SPEC §16) so they
# land where their real views will, and so the kwarg name matches
# RBACMiddleware's resolution contract.
_APP_LAYOUT = "app"
_SETTINGS_LAYOUT = "settings"
_WS_SETTINGS_LAYOUT = "workspace_settings"

# (route, url name, heading, owning issue, layout, permission)
#
# The permission is the one the real view will gate on, from
# apps.members.roles.PERMISSION_KEYS. A placeholder under /w/<uuid>/ is a real
# endpoint: SECURITY-BASELINE §1 requires it to 404 for a member of another
# workspace, and tests/idor.py walks it automatically.
_WORKSPACE_STUBS: list[tuple[str, str, str, str, str, str]] = [
    ("contacts/", "contacts", "Contacts", "#3 (L2-A) and #13 (L4-C)", _APP_LAYOUT, "manage_crm"),
    ("inbox/", "inbox", "Inbox", "#14 (L4-D)", _APP_LAYOUT, "use_inbox"),
    ("sequences/", "sequences", "Sequences", "#22 (L6-A)", _APP_LAYOUT, "edit_flows"),
    ("broadcasts/", "broadcasts", "Broadcasts", "#23 (L6-B)", _APP_LAYOUT, "send_broadcasts"),
    ("settings/fields/", "settings_ws_fields", "Custom Fields", "#3 (L2-A)", _WS_SETTINGS_LAYOUT, "manage_crm"),
    ("settings/tags/", "settings_ws_tags", "Tags", "#3 (L2-A)", _WS_SETTINGS_LAYOUT, "manage_crm"),
]

# Not workspace-scoped, so login is the whole gate.
_GLOBAL_STUBS: list[tuple[str, str, str, str, str]] = [
    ("accounts/preferences/", "settings_preferences", "Preferences", "#31 follow-up", _SETTINGS_LAYOUT),
    ("organization/api-keys/", "settings_org_api_keys", "API Keys", "#25 (L5-F)", _SETTINGS_LAYOUT),
]


def _stub(route: str, name: str, section: str, issue: str, layout: str) -> URLPattern:
    return path(route, views.account_stub, {"section": section, "issue": issue, "layout": layout}, name=name)


def _ws_stub(route: str, name: str, section: str, issue: str, layout: str, permission: str) -> URLPattern:
    return path(
        f"w/<uuid:workspace_id>/{route}",
        views.workspace_stub(permission),
        {"section": section, "issue": issue, "layout": layout},
        name=name,
    )


urlpatterns = [
    path("admin/", admin.site.urls),
    # No trailing slash: SPEC §20 specifies /healthz, and probes are literal.
    path("healthz", views.healthz, name="healthz"),
    # The queue's HTTP drain, for hosts with no always-on worker process
    # (SPEC §15). Not workspace-scoped: it is a deployment-level operations
    # endpoint authenticated by TICK_TOKEN, and it 404s when that is unset.
    path("", include("apps.queueing.urls")),
    # The design system's living style guide. Static markup with no database
    # access and no side effects; see apps.common.views.ui_demo.
    path("ui/", views.ui_demo, name="ui_demo"),
    path("ui/toast/", views.ui_demo_toast, name="ui_demo_toast"),
    # Local routes first: /accounts/signup/ must resolve to the invite-aware
    # view rather than allauth's own. Both live at the same path, so reversing
    # `account_signup` still lands here.
    path("accounts/", include("apps.accounts.urls")),
    *[_stub(*stub) for stub in _GLOBAL_STUBS],
    path("accounts/", include("allauth.urls")),
    # Org-scoped management. One org per user in v1, so no id in the URL.
    path("organization/", include("apps.organizations.urls")),
    path("organization/members/", include("apps.members.urls")),
    # Invite acceptance is unauthenticated — the recipient has no org yet.
    path("", include("apps.members.urls_public")),
    # Per-user, so no workspace prefix: the bell shows every workspace at once
    # (issue #7).
    path("notifications/", include("apps.notifications.urls")),
    # Workspace-scoped routes (SPEC §16). The kwarg name `workspace_id` is
    # RBACMiddleware's resolution contract; do not rename it.
    path("w/<uuid:workspace_id>/", include("apps.workspaces.urls")),
    path("w/<uuid:workspace_id>/settings/credentials/", include("apps.credentials.urls")),
    # Flows own two prefixes under the workspace — the pages at flows/ and
    # SPEC §16's builder data API at api/flows/ — so they mount at the
    # workspace root together under one namespace. See apps/flows/urls.py.
    path("w/<uuid:workspace_id>/", include("apps.flows.urls")),
    *[_ws_stub(*stub) for stub in _WORKSPACE_STUBS],
    path("", account_views.root, name="index"),
]

# Serve uploads from disk in development. Gated on local storage as well as
# DEBUG: with STORAGE_BACKEND=s3 media lives off-origin, and mounting static()
# on an off-origin MEDIA_URL would add a route this project does not want.
if settings.DEBUG and settings.STORAGE_IS_LOCAL:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
