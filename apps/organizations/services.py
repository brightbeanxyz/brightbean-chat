"""Organization-tier writes shared by the session views and the platform API."""

from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.accounts.services import (
    DEFAULT_WORKSPACE_DESCRIPTION,
    DEFAULT_WORKSPACE_NAME,
    ExternalIdInUseError,
    get_or_create_user,
    provision_organization_and_workspace,
)
from apps.billing.entitlements import PlanLimitError, check_can_add_workspace, organization_locked
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import OrgRole, WorkspaceRole
from apps.members.services import add_member_directly
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

MAX_WORKSPACE_NAME = 100


class WorkspaceError(ValueError):
    """A workspace could not be created. The message is shown to the operator."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def single_workspace_refusal(enabled: bool) -> str | None:
    """Why workspaces cannot be added or archived in single-workspace mode, or None."""
    if not enabled:
        return None
    return gettext("Workspaces cannot be added or archived on this installation.")


def create_workspace(org: Any, name: str, *, creator: Any = None) -> Workspace:
    """Create a workspace in ``org``; ``creator`` (if any) becomes its admin.

    The count and the insert are one critical section — see
    ``apps.billing.entitlements.organization_locked``.
    """
    if refusal := single_workspace_refusal(settings.SINGLE_WORKSPACE_MODE):
        raise WorkspaceError(refusal, code="single_workspace")
    name = (name or "").strip()[:MAX_WORKSPACE_NAME]
    if not name:
        raise WorkspaceError(gettext("A workspace needs a name."))
    with organization_locked(org):
        # Inside the lock so two requests cannot both pass the check; the
        # database constraint is the backstop for a writer that bypasses it.
        if Workspace.objects.for_org(org.pk).filter(name=name).exists():
            raise WorkspaceError(gettext("A workspace with that name already exists."), code="duplicate")
        try:
            check_can_add_workspace(org)
        except PlanLimitError as exc:
            raise WorkspaceError(str(exc), code=exc.code) from exc
        try:
            with transaction.atomic():
                workspace = Workspace.objects.create(organization=org, name=name)
        except IntegrityError as exc:
            raise WorkspaceError(gettext("A workspace with that name already exists."), code="duplicate") from exc
        if creator is not None:
            # Or nobody can configure the thing they just made.
            WorkspaceMembership.objects.create(user=creator, workspace=workspace, workspace_role=WorkspaceRole.ADMIN)
    return workspace


def set_workspace_archived(org: Any, workspace: Workspace, *, archived: bool) -> None:
    """Archive or restore ``workspace``; raises :class:`WorkspaceError` on refusal.

    Restoring is the same lever as creating. Without the plan check an
    organization at its workspace limit archives one, creates another, and
    restores the first — the shape every "half-enforced limit" bug takes. The
    write is inside the lock for the reason ``create_workspace``'s is.

    In single-workspace mode archiving is refused, and so is restoring while
    another workspace is active; restoring into an organization with none stays
    open (archived before the mode was switched on), as the only way back.
    """
    with organization_locked(org):
        # Fetched before the lock was taken, so another request may have moved it.
        workspace.refresh_from_db(fields=["is_archived"])
        if refusal := single_workspace_refusal(settings.SINGLE_WORKSPACE_MODE):
            if archived:
                raise WorkspaceError(refusal, code="single_workspace")
            if workspace.is_archived and Workspace.objects.for_org(org.pk).filter(is_archived=False).exists():
                raise WorkspaceError(
                    gettext("Another workspace is already active on this installation."), code="single_workspace"
                )
        if not archived and workspace.is_archived:
            try:
                check_can_add_workspace(org)
            except PlanLimitError as exc:
                raise WorkspaceError(str(exc), code=exc.code) from exc
        workspace.is_archived = archived
        workspace.save(update_fields=["is_archived", "updated_at"])


class OrganizationError(ValueError):
    """An organization could not be provisioned. ``code`` is machine-readable."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


def create_organization(
    *, name: str, owner_email: str, owner_name: str = "", workspace_name: str = "", external_id: str | None = None
) -> tuple[Organization, bool]:
    """Provision a tenant: the organization, its owner and a first workspace.

    Returns ``(organization, created)``. A repeated ``external_id`` finds the
    organization instead of making a second one, which is what makes a retried
    request safe — provided it describes the same tenant: a different name is a
    ``external_id_conflict``, and an organization deleted since is ``deleted``
    (its id is not recycled). The owner is created if they have no account yet,
    and must not already belong to another organization (one per account).
    """
    name = (name or "").strip()
    if not name:
        raise OrganizationError(gettext("An organization needs a name."))
    external_id = (external_id or "").strip() or None
    if external_id and (found := Organization.objects.filter(external_id=external_id).first()):
        return _replay(found, name, owner_email), False
    try:
        with transaction.atomic():
            owner, _ = get_or_create_user(owner_email, owner_name)
            # The owner's row is the lock: two requests for one owner serialise
            # here, so the membership check below is not a read-then-write race.
            User.objects.select_for_update().get(pk=owner.pk)
            if OrgMembership.objects.filter(user=owner).exists():
                raise OrganizationError(
                    gettext("That account already belongs to an organization."), code="other_organization"
                )
            org = provision_organization_and_workspace(
                owner,
                org_name=name,
                workspace_name=(workspace_name or "").strip() or DEFAULT_WORKSPACE_NAME,
                external_id=external_id,
                workspace_description="" if (workspace_name or "").strip() else DEFAULT_WORKSPACE_DESCRIPTION,
            )
            if org is None:
                raise OrganizationError(gettext("The owner account no longer exists."))
    except (IntegrityError, ExternalIdInUseError):
        # A concurrent request provisioned the same external_id first.
        if external_id and (found := Organization.objects.filter(external_id=external_id).first()):
            return _replay(found, name, owner_email), False
        raise OrganizationError(
            gettext("The organization could not be created because of a concurrent change; retry."),
            code="conflict",
        ) from None
    return org, True


def _replay(found: Organization, name: str, owner_email: str) -> Organization:
    if found.deleted_at is not None:
        raise OrganizationError(gettext("An organization with that external id was deleted."), code="deleted")
    owner = OrgMembership.objects.filter(organization=found, org_role=OrgRole.OWNER).select_related("user").first()
    same_owner = owner is None or owner.user.email.lower() == (owner_email or "").strip().lower()
    if found.name != name or not same_owner:
        raise OrganizationError(
            gettext("That external id belongs to an organization with a different name or owner."),
            code="external_id_conflict",
        )
    return found


def add_user(
    org: Organization, *, email: str, name: str, org_role: str, workspace_assignments: Any
) -> tuple[Any, Any, bool, bool]:
    """Create-or-find the account and make it a member of ``org``, all or nothing.

    Returns ``(user, membership, user_created, member_created)``. One transaction,
    so a refused membership (no free seat, a workspace of another organization,
    an account already elsewhere) leaves no new account behind.
    """
    with transaction.atomic():
        user, user_created = get_or_create_user(email, name)
        membership, member_created = add_member_directly(org, user, org_role, workspace_assignments)
    return user, membership, user_created, member_created
