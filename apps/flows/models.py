"""Flows and their versions (SPEC §5, "flows").

Two tenant tables and one invariant worth stating up front: **a flow has at most
one published version.** That is a partial unique index here, not a rule
services remember, because it is what every other layer assumes — the engine
starts "the published version", triggers fire "the published version", and a
second published row would make that phrase ambiguous at the worst moment.

Versioning shape, from the issue: editing writes or updates the *latest draft*,
and publishing flips flags rather than copying a graph. So the newest row is the
draft the builder is editing, unless it is published, in which case the next
edit opens a new one. :mod:`apps.flows.services` owns those transitions —
nothing outside it should be setting ``published`` by hand.

``FlowVersion`` carries its own ``workspace`` foreign key rather than reaching
through ``flow``. SPEC §5 requires one on every tenant table, and it means
``get_scoped_object_or_404(FlowVersion, workspace, ...)`` works directly instead
of via a join that a future query might forget.
"""

from typing import Any

from django.conf import settings
from django.db import models

from apps.common.scoping import WorkspaceScopedModel
from apps.flows.schema import empty_graph

__all__ = ["Flow", "FlowStatus", "FlowVersion"]


class FlowStatus(models.TextChoices):
    """SPEC §5. ``active`` is set by publishing, never by hand."""

    DRAFT = "draft", "Draft"
    ACTIVE = "active", "Active"
    ARCHIVED = "archived", "Archived"


class Flow(WorkspaceScopedModel):
    """One automation. Its runnable content lives in its versions."""

    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=FlowStatus.choices, default=FlowStatus.DRAFT)

    # SPEC §5 says "nullable text". It is an empty string here instead: a
    # nullable CharField gives two different ways to say "no folder", and every
    # grouping query then has to handle both or quietly drop rows. "" is the
    # only "unfiled" there is.
    folder = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        # Not Meta inheritance: WorkspaceScopedModel deliberately declares its
        # managers as class attributes so a subclass writing its own Meta cannot
        # drop them (see apps/common/scoping.py).
        indexes = [
            models.Index(fields=["workspace", "status"], name="flows_flow_ws_status_idx"),
            models.Index(fields=["workspace", "folder"], name="flows_flow_ws_folder_idx"),
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class FlowVersion(WorkspaceScopedModel):
    """One revision of a flow's graph. Monotonic per flow; at most one published."""

    flow = models.ForeignKey(Flow, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    graph_json = models.JSONField(default=empty_graph)
    published = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        # Nothing reads "versions this user created", and a reverse accessor
        # nothing uses is a name every future User relation has to avoid.
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["flow", "version"], name="flows_version_unique_per_flow"),
            # The partial index the issue asks for. Concurrent publishes are
            # already serialised on the flow row in services.publish(); this is
            # the database refusing to hold a state no code should produce, in
            # case some future path forgets the lock.
            models.UniqueConstraint(
                fields=["flow"],
                condition=models.Q(published=True),
                name="flows_one_published_version_per_flow",
            ),
        ]
        indexes = [models.Index(fields=["flow", "-version"], name="flows_version_latest_idx")]
        ordering = ["-version"]

    def __str__(self) -> str:
        return f"{self.flow_id} v{self.version}"

    @property
    def is_draft(self) -> bool:
        return not self.published

    def as_dict(self) -> dict[str, Any]:
        """Version metadata for the builder. The graph is sent separately."""
        return {
            "id": str(self.pk),
            "version": self.version,
            "published": self.published,
            "updated_at": self.updated_at.isoformat(),
        }
