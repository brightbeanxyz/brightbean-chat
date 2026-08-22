"""Segment CRUD for a superuser, and nothing else.

``apps/credentials/admin.py`` sets the house rule: workspace-scoped tenant data
is not registered, because an admin changelist is a cross-tenant view. That rule
holds here — but read its reason, which is that the rows in question are *every
workspace's secrets*. Two of those three clauses fail for ``Segment``: it holds
no secret, and its permission-gated workspace UI is issue #13, two layers away,
so a segment that cannot be created cannot be exercised at all. Issue #3's
acceptance criteria ask for exactly this as the stop-gap.

The clause that does still bind is the strong form — *the admin must not become
a cross-tenant browsing surface for anyone who is merely* ``is_staff`` — so
every hook here is superuser-only, the same five-hook pattern
``PlatformCredentialAdmin`` uses. A superuser on a self-hosted deployment
already has ``manage.py shell`` and the database.

Not registered, with the reason each time:

* ``Contact`` — the largest concentration of personal data in the product
  (SPEC §11.8 consent, issue #29's GDPR work). A changelist with a working
  search box is a cross-tenant PII browser; the real UI is issue #13.
* ``Tag``, ``CustomField`` — their permission-gated workspace UI ships in *this*
  issue, so the credentials rule applies to them verbatim.
* ``ContactTag``, ``CustomFieldValue`` — join and typed-value rows whose
  invariants (derived workspace, exactly one populated column) are held by
  ``services.py``. An admin form is a way to write a row that breaks them.

``apps/contacts/tests/test_admin.py`` asserts that list, so a later PR cannot
quietly widen it.
"""

from typing import Any

from django import forms
from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from apps.contacts.models import Segment


@admin.register(Segment)
class SegmentAdmin(admin.ModelAdmin):
    """The one cross-tenant surface in this app, and only for a superuser.

    The changelist reads ``Segment._default_manager``, which is the plain
    ``all_objects`` (``apps/common/scoping.py`` routes Django's own machinery
    around the enforcing manager on purpose). So this is not a ``.unscoped()``
    call site and CONTRIBUTING's comment rule does not apply to it — but it *is*
    the one place in this app that sees across workspaces, which is why the
    permission hooks below are the whole point of the class.

    ``filter_json`` is validated by ``Segment.clean()``: ``ModelForm._post_clean``
    calls ``full_clean()``, so the condition engine gates the add and change
    pages with no admin-side code, and keeps gating issue #13's segment builder
    when that arrives.
    """

    list_display = ("name", "workspace", "updated_at")
    search_fields = ("name", "workspace__name")
    readonly_fields = ("id", "created_at", "updated_at")
    # The plain foreign-key widget renders every workspace in the deployment in
    # one <select>. WorkspaceAdmin already declares search_fields, which is
    # autocomplete's only requirement.
    autocomplete_fields = ("workspace",)
    formfield_overrides = {models.JSONField: {"widget": forms.Textarea(attrs={"rows": 12, "cols": 80})}}

    def has_module_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)

    def has_view_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_add_permission(self, request: HttpRequest) -> bool:
        return bool(request.user.is_superuser)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return bool(request.user.is_superuser)
