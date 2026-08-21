"""Workspaces — the unit every piece of tenant data hangs off.

Ported from BrightBean Studio's ``apps/workspaces/models.py``, minus its
social-publishing fields: ``approval_workflow_mode`` (and its TextChoices),
``default_hashtags`` and ``default_first_comment`` are Studio product concepts
with no counterpart here (SPEC §1.1 — no approval workflows, no client portal).

Two changes beyond that:

* ``icon`` is a short ``CharField`` holding an emoji or initial, not Studio's
  ``ImageField``. An ImageField needs Pillow, and taking a new runtime
  dependency (SECURITY-BASELINE §10) for one decorative field before the media
  library (#16) exists is not a trade worth making.
* ``(organization, name)`` is unique. Studio has no constraint, so an org can
  hold three workspaces called "Marketing" and the switcher becomes a guess.
"""

from django.db import models

from apps.common.managers import OrgScopedManager
from apps.common.models import BaseModel
from apps.common.validators import validate_hex_color


class Workspace(BaseModel):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="workspaces",
    )
    name = models.CharField(max_length=100)
    icon = models.CharField(max_length=8, blank=True, default="", help_text="A single emoji, shown in the switcher.")
    description = models.CharField(max_length=500, blank=True, default="")
    timezone = models.CharField(max_length=63, blank=True, default="")
    primary_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    secondary_color = models.CharField(max_length=7, blank=True, default="", validators=[validate_hex_color])
    is_archived = models.BooleanField(default=False)

    objects = OrgScopedManager()

    class Meta:
        db_table = "workspaces_workspace"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="workspace_unique_name_per_org"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def effective_timezone(self) -> str:
        """The workspace's own timezone, or the organization's default."""
        return self.timezone or self.organization.default_timezone
