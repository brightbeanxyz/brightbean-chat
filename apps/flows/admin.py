"""Admin for flows.

Read-mostly on purpose. Version numbering and the one-published-version rule are
enforced by :mod:`apps.flows.services` inside a row lock; an admin form that let
someone tick ``published`` on a second row would be editing around that lock, and
the database would answer with an IntegrityError rather than anything useful.

``workspace`` is read-only on both models, which matters more than it looks.
Every service query scopes on ``workspace_id``, and a ``FlowVersion`` carries
its own copy of it rather than reaching through ``flow`` (SPEC §5). Moving
either row on its own therefore does not raise anything — it just makes
``_versions(flow)`` stop returning that version, which can leave an active flow
whose published version has become invisible to the code that reads it.
"""

from typing import Any

from django.contrib import admin

from apps.flows.models import Flow, FlowExecution, FlowVersion


@admin.register(Flow)
class FlowAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "status", "folder", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "folder")
    readonly_fields = ("workspace",)
    # The plain manager, which is what ModelAdmin uses anyway: the admin is
    # cross-tenant by design and the scoped manager would refuse to run here.
    ordering = ("workspace", "name")
    # list_display renders the workspace per row; without this the changelist
    # issues one query per flow to fetch it.
    list_select_related = ("workspace",)


@admin.register(FlowVersion)
class FlowVersionAdmin(admin.ModelAdmin):
    list_display = ("flow", "version", "published", "created_by", "updated_at")
    list_filter = ("published",)
    readonly_fields = ("workspace", "flow", "version", "graph_json", "published", "created_by")
    list_select_related = ("flow", "created_by")

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False


@admin.register(FlowExecution)
class FlowExecutionAdmin(admin.ModelAdmin):
    """Entirely read-only, and that is the point.

    An execution is a state machine whose every transition is a write inside a
    contact advisory lock (SPEC §9.6). An admin form editing ``status`` or
    ``current_node_id`` would be a write with no lock at all, racing a worker
    that is mid-step — and the visible symptom would be a contact getting two
    messages, days later, with nothing in the logs to connect it to a form
    somebody saved. Operators need to *read* these rows constantly ("where did
    this contact stop, and why"); nobody needs to edit one.

    ``last_error`` is already scrubbed and capped on write
    (``apps.flows.engine.runner``), which matters because this page is where it
    is read.
    """

    list_display = ("flow", "contact", "status", "current_node_id", "blocks_since_pause", "preview", "updated_at")
    list_filter = ("status", "preview")
    search_fields = ("current_node_id", "started_by")
    date_hierarchy = "created_at"
    # Every column, deliberately: see the class docstring.
    readonly_fields = tuple(field.name for field in FlowExecution._meta.fields)
    # The changelist renders four related objects per row.
    list_select_related = ("flow", "contact", "flow_version", "channel_connection")
    ordering = ("-updated_at",)

    def has_add_permission(self, request: Any, obj: Any = None) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return False
