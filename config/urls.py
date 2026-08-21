from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounts import views as account_views
from apps.common import views

urlpatterns = [
    path("admin/", admin.site.urls),
    # No trailing slash: SPEC §20 specifies /healthz, and probes are literal.
    path("healthz", views.healthz, name="healthz"),
    # Local routes first: /accounts/signup/ must resolve to the invite-aware
    # view rather than allauth's own. Both live at the same path, so reversing
    # `account_signup` still lands here.
    path("accounts/", include("apps.accounts.urls")),
    path("accounts/", include("allauth.urls")),
    # Org-scoped management. One org per user in v1, so no id in the URL.
    path("organization/", include("apps.organizations.urls")),
    path("organization/members/", include("apps.members.urls")),
    # Invite acceptance is unauthenticated — the recipient has no org yet.
    path("", include("apps.members.urls_public")),
    # Workspace-scoped routes (SPEC §16). The kwarg name `workspace_id` is
    # RBACMiddleware's resolution contract; do not rename it.
    path("w/<uuid:workspace_id>/", include("apps.workspaces.urls")),
    path("w/<uuid:workspace_id>/settings/credentials/", include("apps.credentials.urls")),
    path("", account_views.root, name="index"),
]

# Serve uploads from disk in development. Gated on local storage as well as
# DEBUG: with STORAGE_BACKEND=s3 media lives off-origin, and mounting static()
# on an off-origin MEDIA_URL would add a route this project does not want.
if settings.DEBUG and settings.STORAGE_IS_LOCAL:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
