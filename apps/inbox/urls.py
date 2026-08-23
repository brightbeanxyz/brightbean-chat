"""Inbox routes, mounted at ``/w/<uuid:workspace_id>/inbox/`` (SPEC §16).

The kwarg name ``workspace_id`` is ``RBACMiddleware``'s entire resolution
contract; a route that spells it differently silently loses the membership check
and the 404.

Every route is named, because ``tests/idor.py`` reverses by name and refuses to
skip a tenant route that has none — and every one of these carries a
``conversation_id``, which is the definition of a route that sweep has to reach.
"""

from django.urls import path

from apps.inbox import views

app_name = "inbox"

urlpatterns = [
    path("", views.inbox, name="list"),
    path("conversations/", views.rows, name="rows"),
    path("<uuid:conversation_id>/", views.thread, name="thread"),
    path("<uuid:conversation_id>/messages/", views.messages, name="messages"),
    path("<uuid:conversation_id>/composer/", views.composer, name="composer"),
    path("<uuid:conversation_id>/header/", views.header, name="header"),
    path("<uuid:conversation_id>/sidebar/", views.sidebar, name="sidebar"),
    path("<uuid:conversation_id>/send/", views.send, name="send"),
    path("<uuid:conversation_id>/note/", views.note, name="note"),
    path("<uuid:conversation_id>/assign/", views.assign, name="assign"),
    path("<uuid:conversation_id>/state/", views.set_state, name="state"),
    path("<uuid:conversation_id>/pause/", views.pause, name="pause"),
    path("<uuid:conversation_id>/stop/", views.stop_automation, name="stop"),
    path("<uuid:conversation_id>/tags/", views.tags, name="tags"),
    path("<uuid:conversation_id>/messages/<uuid:message_id>/retry/", views.retry, name="retry"),
]
