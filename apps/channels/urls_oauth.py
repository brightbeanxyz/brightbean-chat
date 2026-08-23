"""The Meta OAuth callback, mounted at ``/oauth/meta/`` (issue #18).

Separate from ``urls.py`` because it cannot live under ``/w/<workspace_id>/``:
Meta whitelists one exact ``redirect_uri`` per app, and a per-workspace path
would mean one whitelist entry per tenant — impossible for a self-hoster and
pointless for anyone else. The workspace comes from the signed ``state``
instead, and ``apps.channels.views_messenger.messenger_oauth_callback`` does the
membership and permission checks the middleware would otherwise have done from
the URL.

Separate from ``urls_webhooks.py`` too, and for the opposite reason: a webhook is
unauthenticated and a signature is its credential, while this route is
``@login_required`` and the browser arriving on it is a signed-in operator's.

The route carries **no tenant-shaped kwarg**, so ``tests/idor.py`` skips it the
way it skips every workspace-neutral route — there is no id on it to fuzz. What
stands in for the sweep is the state check, which
``apps/channels/tests/test_messenger_connect.py::TestStateIsTheBoundary`` holds:
a forged, expired, foreign-purpose or foreign-workspace state is refused, and a
valid state for a workspace the signed-in user does not administer answers 404.
"""

from django.urls import path

from apps.channels import views_messenger

urlpatterns = [
    path("callback/", views_messenger.messenger_oauth_callback, name="messenger_oauth_callback"),
]
