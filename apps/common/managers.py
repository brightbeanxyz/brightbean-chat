"""Scoped model managers.

Ported from BrightBean Studio's ``apps/common/managers.py``, minus its
``OrgScopedModel`` / ``WorkspaceScopedModel`` abstract models: those declare
foreign keys to ``organizations.Organization`` and ``workspaces.Workspace``,
which do not exist yet and belong to issue #31 along with the rest of tenancy.

Issue #31 builds the enforcing workspace-scoped base manager on top of these
(SECURITY-BASELINE §1: every queryset on tenant data goes through it, and
cross-workspace access returns 404, never 403).
"""

from typing import Any

from django.db import models


class OrgScopedManager(models.Manager):
    """Manager that filters queries by ``organization_id``.

    Usage::

        MyModel.objects.for_org(org_id).all()
    """

    def for_org(self, organization_id: Any) -> models.QuerySet:
        return self.get_queryset().filter(organization_id=organization_id)


class WorkspaceScopedManager(models.Manager):
    """Manager that filters queries by ``workspace_id``.

    Usage::

        MyModel.objects.for_workspace(workspace_id).all()
    """

    def for_workspace(self, workspace_id: Any) -> models.QuerySet:
        return self.get_queryset().filter(workspace_id=workspace_id)
