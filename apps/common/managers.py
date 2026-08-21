"""Org-scoped model manager.

Ported from BrightBean Studio's ``apps/common/managers.py``. Studio's
``WorkspaceScopedManager`` is deliberately **not** here: the enforcing one lives
in :mod:`apps.common.scoping`, and two classes of the same name — one of which
silently does not enforce anything — is a trap rather than a convenience.
Studio's ``OrgScopedModel`` / ``WorkspaceScopedModel`` abstract models are not
ported either; ``apps.common.scoping.WorkspaceScopedModel`` replaces the latter.

Organizations are the tenant root, so org-scoped models carry no enforcement:
there is no outer tenant to leak across, and ``Organization`` itself has to be
queryable without a scope. Tenant data hangs off a workspace and goes through
``apps.common.scoping`` (SECURITY-BASELINE §1).
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
