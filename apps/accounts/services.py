"""Account provisioning.

BrightBean Studio provisions from **two** signal receivers: a ``post_save`` on
every ``User`` row, plus allauth's ``user_signed_up``. Because ``post_save``
runs first, an invited signup gets a default "My Organization" created and then
has to *delete it again* — matched by that literal name string — before joining
the org that actually invited them. The same ordering is why three of Studio's
test modules carry a copy-pasted ``_make_user()`` that creates a user, then
tears down the org, workspace and membership the signal just made.

Here the ``post_save`` receiver is gone (brief, deviation 8). Provisioning
happens at the two points that genuinely mean "this person needs somewhere to
work":

* allauth's ``user_signed_up`` — the real signup path, email or social. The
  invite branch simply accepts the invitation; there is nothing to clean up
  because nothing was created.
* :func:`ensure_provisioned`, called by the root router view, which covers
  accounts made outside signup — ``createsuperuser``, the admin, a shell.

The consequence for tests is the point: ``User.objects.create_user(...)`` now
creates exactly a user, so no test needs a teardown helper.
"""

import logging
from typing import Any

from django.db import transaction

logger = logging.getLogger(__name__)

DEFAULT_ORG_NAME = "My Organization"
DEFAULT_WORKSPACE_NAME = "My Workspace"


@transaction.atomic
def provision_organization_and_workspace(user: Any) -> Any:
    """Give ``user`` an organization, a workspace and owner/admin roles.

    Idempotent: a user who already belongs to an organization is left alone, so
    every caller can be unconditional.
    """
    from django.utils import timezone

    from apps.accounts.models import User
    from apps.members.models import OrgMembership, WorkspaceMembership
    from apps.members.roles import OrgRole, WorkspaceRole
    from apps.organizations.models import Organization
    from apps.workspaces.models import Workspace

    # Lock the user row for the rest of the transaction before reading the
    # guard. Without it two concurrent first visits — two tabs, or a prefetch
    # racing the click — both see no membership and both provision, leaving the
    # account in two organizations with nothing to say which is real. Studio ran
    # this inside the user-creation transaction, so moving it to a request
    # handler is what made the window reachable.
    if not User.objects.select_for_update().filter(pk=user.pk).exists():
        return None  # deleted between the request arriving and getting the lock

    existing = OrgMembership.objects.filter(user=user).select_related("organization").first()
    if existing:
        return existing.organization

    org = Organization.objects.create(name=DEFAULT_ORG_NAME, default_timezone="UTC")
    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgRole.OWNER,
        accepted_at=timezone.now(),
    )
    workspace = Workspace.objects.create(
        organization=org,
        name=DEFAULT_WORKSPACE_NAME,
        description="Your default workspace. Rename it anytime.",
    )
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role=WorkspaceRole.ADMIN)

    user.last_workspace_id = workspace.pk
    user.save(update_fields=["last_workspace_id"])
    return org


def ensure_provisioned(user: Any) -> None:
    """Provision on first authenticated visit if signup never did.

    Covers ``createsuperuser`` and any other path that writes a ``User`` row
    directly. Idempotent and one query for everybody who already has an org.
    """
    from apps.members.models import OrgMembership

    if OrgMembership.objects.filter(user=user).exists():
        return
    logger.info("Provisioning a default organization for user %s on first visit", user.pk)
    provision_organization_and_workspace(user)
