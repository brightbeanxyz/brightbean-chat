"""API key management, mounted at ``/organization/api-keys/``.

Deliberately **no** ``app_name``. The list route keeps the URL name
``settings_org_api_keys``, which ``config/urls.py`` reserved for this issue and
``apps/common/context_processors.py`` reverses for the sidebar row — the owning
issue swaps the view, not the nav registry.

``api_key_id`` is the kwarg name because ``tests/idor.py`` resolves tenant
objects by kwarg name: registering a resolver for it puts these routes into the
cross-tenant sweep automatically, which is what an org-tier page naming a
workspace's object needs.
"""

from django.urls import path

from apps.api import views_keys

urlpatterns = [
    path("", views_keys.key_list, name="settings_org_api_keys"),
    path("issue/", views_keys.issue_key, name="api_keys_issue"),
    path("<uuid:api_key_id>/revoke/", views_keys.revoke_key, name="api_keys_revoke"),
]
