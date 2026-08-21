"""Enforcing workspace scoping for tenant models (SECURITY-BASELINE §1).

BrightBean Studio's ``WorkspaceScopedManager`` is a ``.for_workspace(id)``
convenience method and nothing else. Its docstring says "auto-filters", but
``get_queryset()`` is not overridden, so ``Model.objects.all()`` still returns
every tenant's rows. Isolation there rests entirely on every view author
remembering the filter — and the one that forgets leaks silently, with a 200 and
somebody else's data. It is not ported; this is the enforcing version, and it
carries the name so there is only ever one thing to import. A tenant model inherits
:class:`WorkspaceScopedModel`; its ``objects`` manager hands out querysets that
**refuse to execute** until they have been scoped:

    Contact.objects.filter(status="active")            # raises UnscopedQueryError
    Contact.objects.for_workspace(ws).filter(...)      # fine
    Contact.objects.unscoped().count()                 # fine, and greppable

The check fires at execution, not at ``.filter()``, so building a queryset in
pieces still works; only running one unscoped is an error. Every terminal
operation is guarded, not just iteration — ``count()``, ``exists()``,
``update()``, ``delete()``, ``aggregate()`` and ``iterator()`` all reach the
database without going through ``_fetch_all``, and an unscoped ``update()`` is
the most damaging of the lot.

Django's own machinery is deliberately routed around the guard:
``Meta.default_manager_name`` and ``Meta.base_manager_name`` both point at the
plain ``all_objects``, so the admin, cascade deletes, serialization and reverse
related-manager access (``workspace.contacts.all()``, which is already scoped by
construction) keep working. ``objects`` is what application code types, and
that is where the enforcement belongs.

``.unscoped()`` is the escape hatch. It exists because cross-tenant reads are
occasionally legitimate (housekeeping sweeps, a superuser admin action). Every
call site must carry a comment saying why — see ``CONTRIBUTING.md``.
"""

from typing import Any, Self
from uuid import UUID

from django.db import models

from apps.common.models import BaseModel

__all__ = [
    "UnscopedQueryError",
    "WorkspaceScopedManager",
    "WorkspaceScopedModel",
    "WorkspaceScopedQuerySet",
]


class UnscopedQueryError(RuntimeError):
    """Raised when a tenant queryset is executed without a workspace scope."""


def _workspace_id(workspace: Any) -> Any:
    """Accept a Workspace, a UUID or a string and return the id to filter on."""
    if workspace is None:
        raise ValueError(
            "for_workspace() needs a workspace. Passing None would filter on "
            "workspace_id IS NULL, which quietly matches nothing instead of failing."
        )
    if isinstance(workspace, UUID | str):
        return workspace
    return workspace.pk


class WorkspaceScopedQuerySet(models.QuerySet):
    """A queryset that will not touch the database until it has been scoped."""

    # Class-level default so freshly constructed querysets start unscoped.
    _workspace_scoped = False

    def _clone(self) -> Self:
        clone = super()._clone()  # type: ignore[misc]
        clone._workspace_scoped = self._workspace_scoped
        return clone

    def for_workspace(self, workspace: Any) -> Self:
        """Restrict to one workspace and mark the queryset safe to execute."""
        clone = self.filter(workspace_id=_workspace_id(workspace))
        clone._workspace_scoped = True
        return clone

    def unscoped(self) -> Self:
        """Deliberately query across every tenant.

        Only for code that genuinely operates on all workspaces at once —
        housekeeping jobs, superuser admin actions, migrations. Every call site
        needs a comment saying why (``CONTRIBUTING.md``).
        """
        clone = self._chain()  # type: ignore[attr-defined]
        clone._workspace_scoped = True
        return clone

    def _assert_scoped(self, operation: str) -> None:
        if self._workspace_scoped:
            return
        raise UnscopedQueryError(
            f"{self.model.__name__}.{operation}() was called without a workspace scope. "
            f"Use {self.model.__name__}.objects.for_workspace(workspace) — or "
            f".unscoped() with a comment explaining why crossing tenants is correct here. "
            f"See docs/SECURITY-BASELINE.md §1."
        )

    # -- terminal operations ------------------------------------------------
    # _fetch_all covers iteration, len(), get(), first(), latest() and friends.
    # The rest each reach the database on their own.

    def _fetch_all(self) -> None:
        if self._result_cache is None:
            self._assert_scoped("iteration")
        super()._fetch_all()

    def iterator(self, *args: Any, **kwargs: Any) -> Any:
        self._assert_scoped("iterator")
        return super().iterator(*args, **kwargs)

    def count(self) -> int:
        if self._result_cache is None:
            self._assert_scoped("count")
        return super().count()

    def exists(self) -> bool:
        if self._result_cache is None:
            self._assert_scoped("exists")
        return super().exists()

    def aggregate(self, *args: Any, **kwargs: Any) -> Any:
        self._assert_scoped("aggregate")
        return super().aggregate(*args, **kwargs)

    def update(self, **kwargs: Any) -> int:
        self._assert_scoped("update")
        return super().update(**kwargs)

    def delete(self) -> Any:
        self._assert_scoped("delete")
        return super().delete()

    def in_bulk(self, *args: Any, **kwargs: Any) -> Any:
        self._assert_scoped("in_bulk")
        return super().in_bulk(*args, **kwargs)


WorkspaceScopedManager = models.Manager.from_queryset(WorkspaceScopedQuerySet)


class WorkspaceScopedModel(BaseModel):
    """Abstract base for every model that carries tenant data.

    SPEC §5: "All tenant tables have ``workspace_id`` FK with index."
    SECURITY-BASELINE §1: "Every queryset on tenant data goes through the
    workspace-scoped base manager."

    ``objects`` is what application code types, and it enforces. ``all_objects``
    is the plain manager Django's own machinery needs, and it is declared
    **first on purpose**: ``Meta.default_manager_name`` is unset, so Django takes
    the earliest-created manager as ``_default_manager``, which is what the
    admin, serialization and reverse related access (``workspace.contacts.all()``
    — already scoped by construction) all go through. Reordering these two lines
    would poison those paths; ``apps.common.checks.check_workspace_scoped_models``
    fails the build if anyone does.

    Declaration order rather than ``Meta`` because a subclass that writes its own
    ``class Meta:`` without inheriting this one silently drops ``Meta`` options,
    and "silently drops the safety property" is the failure mode this whole
    module exists to remove.

    The ``workspace`` FK is indexed by Django's ForeignKey default, which is the
    index SPEC §5 asks for.
    """

    all_objects = models.Manager()
    objects = WorkspaceScopedManager()

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="%(class)ss",
    )

    class Meta:
        abstract = True
