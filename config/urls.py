from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from apps.common import views

# The sidebar's destinations. Every one is a stub until its issue lands; the
# navigation itself is complete from day one so the shell is reviewable and so
# a later issue swaps a view rather than editing the nav (see
# apps.common.context_processors.MAIN_NAV / SETTINGS_NAV).
_APP_LAYOUT = "base.html"
_SETTINGS_LAYOUT = "layouts/settings.html"
_WS_SETTINGS_LAYOUT = "layouts/workspace_settings.html"

# (route, url name, heading, owning issue, layout)
_STUBS: list[tuple[str, str, str, str, str]] = [
    ("dashboard/", "dashboard", "Dashboard", "#3 (L2-A) and #14 (L4-D)", _APP_LAYOUT),
    ("contacts/", "contacts", "Contacts", "#3 (L2-A) and #13 (L4-C)", _APP_LAYOUT),
    ("flows/", "flows", "Flows", "#6 (L2-D) and #10 (L3-C)", _APP_LAYOUT),
    ("inbox/", "inbox", "Inbox", "#14 (L4-D)", _APP_LAYOUT),
    ("sequences/", "sequences", "Sequences", "#22 (L6-A)", _APP_LAYOUT),
    ("broadcasts/", "broadcasts", "Broadcasts", "#23 (L6-B)", _APP_LAYOUT),
    ("settings/profile/", "settings_profile", "Profile", "#31 (L1-A)", _SETTINGS_LAYOUT),
    ("settings/preferences/", "settings_preferences", "Preferences", "#31 (L1-A)", _SETTINGS_LAYOUT),
    ("settings/organization/", "settings_org_general", "Organization", "#31 (L1-A)", _SETTINGS_LAYOUT),
    ("settings/organization/workspaces/", "settings_org_workspaces", "Workspaces", "#31 (L1-A)", _SETTINGS_LAYOUT),
    ("settings/organization/members/", "settings_org_members", "Team Members", "#31 (L1-A)", _SETTINGS_LAYOUT),
    ("settings/organization/api-keys/", "settings_org_api_keys", "API Keys", "#25 (L5-F)", _SETTINGS_LAYOUT),
    ("settings/workspace/", "settings_ws_general", "Workspace", "#31 (L1-A)", _WS_SETTINGS_LAYOUT),
    ("settings/workspace/channels/", "settings_ws_channels", "Channels", "#4 (L2-B)", _WS_SETTINGS_LAYOUT),
    ("settings/workspace/fields/", "settings_ws_fields", "Custom Fields", "#3 (L2-A)", _WS_SETTINGS_LAYOUT),
    ("settings/workspace/tags/", "settings_ws_tags", "Tags", "#3 (L2-A)", _WS_SETTINGS_LAYOUT),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    # No trailing slash: SPEC §20 specifies /healthz, and probes are literal.
    path("healthz", views.healthz, name="healthz"),
    # The design system's living style guide. Static markup with no database
    # access and no side effects; see apps.common.views.ui_demo.
    path("ui/", views.ui_demo, name="ui_demo"),
    path("ui/toast/", views.ui_demo_toast, name="ui_demo_toast"),
    *[
        path(route, views.coming_soon, {"section": section, "issue": issue, "layout": layout}, name=name)
        for route, name, section, issue, layout in _STUBS
    ],
    path("", views.index, name="index"),
]

# Serve uploads from disk in development. Gated on local storage as well as
# DEBUG: with STORAGE_BACKEND=s3 media lives off-origin, and mounting static()
# on an off-origin MEDIA_URL would add a route this project does not want.
if settings.DEBUG and settings.STORAGE_IS_LOCAL:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
