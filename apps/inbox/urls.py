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
    # Inbound media held as a platform identifier, resolved on demand. Under the
    # workspace prefix and behind a session on purpose — the reader is a team
    # member, not a platform, so membership is the credential and there is no
    # token (apps/channels/media.py sets that out against the /m/ precedent).
    path("<uuid:conversation_id>/messages/<uuid:message_id>/media/<int:index>/", views.media, name="media"),
    # --- issue #24 -----------------------------------------------------------
    # Labels, reminders and scheduled replies on one thread. Every one carries a
    # conversation_id, so tests/idor.py reaches them all; the three new id kwargs
    # each get a resolver there.
    path("<uuid:conversation_id>/labels/", views.add_label, name="add_label"),
    path("<uuid:conversation_id>/labels/<uuid:label_id>/remove/", views.remove_label, name="remove_label"),
    path("<uuid:conversation_id>/reminders/", views.create_reminder, name="create_reminder"),
    path(
        "<uuid:conversation_id>/reminders/<uuid:reminder_id>/cancel/",
        views.cancel_reminder,
        name="cancel_reminder",
    ),
    path("<uuid:conversation_id>/scheduled/", views.create_scheduled_reply, name="create_scheduled_reply"),
    path(
        "<uuid:conversation_id>/scheduled/<uuid:scheduled_reply_id>/",
        views.update_scheduled_reply,
        name="update_scheduled_reply",
    ),
    path(
        "<uuid:conversation_id>/scheduled/<uuid:scheduled_reply_id>/cancel/",
        views.cancel_scheduled_reply,
        name="cancel_scheduled_reply",
    ),
    # The list's bulk-select action. No conversation_id in the path — it posts a
    # set of them — so `label_id` is what makes the sweep reach it, which is why
    # that kwarg is registered rather than treated as neutral.
    path("bulk/labels/", views.bulk_label, name="bulk_label"),
    # Settings. Under this app's own prefix rather than config/urls.py's, so the
    # two Layer-6 siblings and this branch never touch that file.
    path("settings/labels/", views.label_settings, name="label_settings"),
    path("settings/labels/rows/", views.label_rows, name="label_rows"),
    path("settings/labels/create/", views.label_create, name="label_create"),
    path("settings/labels/<uuid:label_id>/update/", views.label_update, name="label_update"),
    path("settings/labels/<uuid:label_id>/delete/", views.label_delete, name="label_delete"),
    path("settings/rules/", views.rule_settings, name="rule_settings"),
    path("settings/rules/rows/", views.rule_rows, name="rule_rows"),
    path("settings/rules/form/", views.rule_form, name="rule_form"),
    path("settings/rules/save/", views.rule_save, name="rule_save"),
    path("settings/rules/reorder/", views.rule_reorder, name="rule_reorder"),
    path("settings/rules/test/", views.rule_test, name="rule_test"),
    path("settings/rules/<uuid:rule_id>/toggle/", views.rule_toggle, name="rule_toggle"),
    path("settings/rules/<uuid:rule_id>/delete/", views.rule_delete, name="rule_delete"),
]
