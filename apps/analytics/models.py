"""The analytics tables (SPEC §5, §18).

SPEC §5 lists exactly one:

    ``node_stat_daily``: flow_id, node_id, date, sent, delivered, failed,
    clicked. Upserted counters, no per-event rows in v1 beyond ``message``.

That last clause is the whole design. There is no click table, no open table and
no per-contact history: a click increments a column and is then forgotten, which
is what keeps v1's analytics a page of numbers rather than an event store. SPEC
§18 repeats the boundary from the other side — "no funnels, no pixel-on-websites,
no UTM builder".

--------------------------------------------------------------------------
Dates are UTC
--------------------------------------------------------------------------

The bucket is ``timezone.now().date()``, not the workspace's local date. A
workspace-local bucket would put a ``Workspace`` load on the send path — the
counter is written from inside :mod:`apps.messaging.services`, once per message —
to move a handful of rows across a midnight boundary in a trend chart. The rule
is written down here so a reader of the chart knows which midnight it is.

--------------------------------------------------------------------------
Why the second table is here and not on ``Workspace``
--------------------------------------------------------------------------

:class:`TrackingSettings` holds the per-workspace email toggles issue #26 asks
for. It is a row in this app rather than two columns on
``apps.workspaces.models.Workspace`` because a workspace that never opens the
analytics section should not carry them, and because a migration for this
issue's feature belongs in this issue's app.
"""

from typing import Any, ClassVar

from django.db import models

from apps.common.models import BaseModel
from apps.common.scoping import WorkspaceScopedModel

__all__ = ["COUNTER_FIELDS", "NodeStatDaily", "TrackingSettings"]

#: The four counters SPEC §5 names, in its order. Written down once because the
#: upsert in :mod:`apps.analytics.counters` builds its statement from this tuple
#: and the API payload is keyed by it — a fifth column added to the model and
#: forgotten here would be a column nothing ever increments.
COUNTER_FIELDS: tuple[str, ...] = ("sent", "delivered", "failed", "clicked")


class NodeStatDaily(WorkspaceScopedModel):
    """One flow node's counters for one UTC day.

    Never created or updated through the ORM by application code: see
    :func:`apps.analytics.counters.bump`, which does it in a single
    ``INSERT … ON CONFLICT … DO UPDATE`` so that two workers incrementing the
    same row cannot lose a count between a read and a write.
    """

    flow = models.ForeignKey("flows.Flow", on_delete=models.CASCADE, related_name="node_stats")
    #: The node's id inside ``flow_version.graph_json``. Matches the width of
    #: ``FlowExecution.current_node_id``, which is where these values come from.
    node_id = models.CharField(max_length=64)
    #: The UTC date the event landed on. See the module docstring.
    date = models.DateField()

    sent = models.PositiveIntegerField(default=0)
    delivered = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    clicked = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "analytics_node_stat_daily"
        constraints: ClassVar[list[Any]] = [
            # The upsert's conflict target. ``flow`` implies ``workspace``, so the
            # tenant column is not in the key — including it would let two rows
            # for one (flow, node, day) exist if a flow ever moved workspace,
            # which is precisely the split the counter must not have.
            models.UniqueConstraint(fields=["flow", "node_id", "date"], name="node_stat_unique_flow_node_date"),
        ]
        indexes: ClassVar[list[Any]] = [
            # The stats API and the flow page: one flow, a date range.
            models.Index(fields=["flow", "date"], name="node_stat_flow_date_idx"),
            # The workspace overview: every flow, a date range.
            models.Index(fields=["workspace", "date"], name="node_stat_ws_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.flow_id}/{self.node_id} on {self.date}"


class TrackingSettings(BaseModel):
    """Whether this workspace rewrites email links and embeds an open pixel.

    ``BaseModel`` rather than ``WorkspaceScopedModel``: this is one row per
    workspace, always fetched *by* workspace and never listed, the same shape and
    the same reasoning as ``apps.messaging.models.SendBucket``. The one-to-one is
    the tenant boundary.

    **Both default to off.** A self-hoster who follows the README must not end up
    mailing tracking pixels nobody asked for; SPEC §18's counters work without
    either toggle, because URL buttons carry their own ``/c/`` wrapper on every
    platform and an author who added a button asked for a link that is counted.
    Rewriting the anchors inside somebody's authored HTML, and adding an image
    that reports back when a message is *displayed*, are the two that a workspace
    opts into.
    """

    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="tracking_settings",
    )
    #: Rewrite ``<a href>`` inside an authored email body through ``/c/``.
    wrap_email_links = models.BooleanField(default=False)
    #: Append the 1×1 ``/o/`` pixel to an authored email body.
    open_pixel = models.BooleanField(default=False)

    class Meta:
        db_table = "analytics_tracking_settings"

    def __str__(self) -> str:
        return f"Tracking settings for workspace {self.workspace_id}"
