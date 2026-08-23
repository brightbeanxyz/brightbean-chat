"""OAuth callbacks, mounted at ``/channels/`` (SPEC §6.3, §6.4).

Not under ``/w/<workspace_id>/``, and that is forced rather than chosen: Meta
matches a redirect URI against the app's configuration **exactly**, so one app
has one callback URL for the whole deployment. Putting a workspace id in it
would mean registering a new redirect URI per tenant, which is not something a
self-hoster can automate.

What replaces the URL's tenancy is a signed ``state`` — see
:mod:`apps.channels.instagram_oauth` — carrying the workspace *and* the user the
flow was started by, both re-checked in the view before anything is exchanged or
written. The routes are ``login_required``, so an anonymous caller is bounced to
the login page and never reaches the exchange at all.

``tests/idor.py`` skips these: they carry no tenant-identifying kwarg, so there
is nothing for the sweep to substitute another tenant's id into. The boundary is
covered directly instead, by the cross-tenant class in
``apps/channels/tests/test_instagram_connect.py``.

L5-B's Messenger callback belongs here too, as one more line.
"""

from django.urls import path

from apps.channels import views_instagram

urlpatterns = [
    path("instagram/callback/", views_instagram.instagram_callback, name="instagram_callback"),
]
