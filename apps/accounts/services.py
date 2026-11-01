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

from django.db import IntegrityError, transaction

logger = logging.getLogger(__name__)


class ExternalIdInUseError(ValueError):
    """Another organization already carries this ``external_id``."""


DEFAULT_ORG_NAME = "My Organization"
DEFAULT_WORKSPACE_NAME = "My Workspace"
DEFAULT_WORKSPACE_DESCRIPTION = "Your default workspace. Rename it anytime."


@transaction.atomic
def provision_organization_and_workspace(
    user: Any,
    *,
    org_name: str = DEFAULT_ORG_NAME,
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    external_id: str | None = None,
    workspace_description: str = DEFAULT_WORKSPACE_DESCRIPTION,
) -> Any:
    """Give ``user`` an organization, a workspace and owner/admin roles.

    The names and ``external_id`` are for callers that provision on someone's
    behalf (the platform API); self-service signup takes the defaults.

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

    external_id = external_id or None
    try:
        with transaction.atomic():
            org = Organization.objects.create(name=org_name, default_timezone="UTC", external_id=external_id)
    except IntegrityError as exc:
        if external_id is None:
            raise
        # A domain answer instead of a bare constraint error: the caller can tell
        # "that id is taken" (and look the organization up) from a real failure.
        raise ExternalIdInUseError(external_id) from exc
    OrgMembership.objects.create(
        user=user,
        organization=org,
        org_role=OrgRole.OWNER,
        accepted_at=timezone.now(),
    )
    workspace = Workspace.objects.create(
        organization=org,
        name=workspace_name,
        description=workspace_description,
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


def pending_invitation(request: Any) -> Any:
    """The unaccepted, unexpired invitation whose token the session holds, or None.

    Read-only: the token stays in the session until the signup that uses it
    succeeds, so a refusal does not cost the visitor their invitation.
    """
    from apps.members.models import Invitation
    from apps.members.signals_keys import PENDING_INVITE_SESSION_KEY

    token = request.session.get(PENDING_INVITE_SESSION_KEY) if request is not None else None
    invitation = Invitation.objects.for_token(token).filter(accepted_at__isnull=True).first() if token else None
    return None if invitation is None or invitation.is_expired else invitation


def signup_is_open(request: Any) -> bool:
    """Whether this visitor may create an account: always under ``SIGNUP=open``,
    otherwise only while their session holds a valid invitation. ``or`` order
    matters: an open installation never queries."""
    from django.conf import settings

    return settings.SIGNUP == "open" or pending_invitation(request) is not None


def is_sso(sociallogin: Any) -> bool:
    """Whether this social login came through the single sign-on provider."""
    from django.conf import settings

    return sociallogin.account.provider == settings.OIDC_PROVIDER_ID


def is_invite_only(sociallogin: Any = None) -> bool:
    """Whether a new account here is created only to honour an invitation.

    ``SIGNUP=invite`` says so for every signup (pass ``None`` for the local
    form); ``OIDC_SIGNUP=invite`` says so for single sign-on alone. These are
    the signups that get the strict path: the user and the invitation are
    accepted in one transaction, and the address must be the invited one."""
    from django.conf import settings

    if settings.SIGNUP == "invite":
        return True
    return sociallogin is not None and is_sso(sociallogin) and settings.OIDC_SIGNUP == "invite"


class InvalidEmailError(ValueError):
    """The address is not a valid email."""


def get_or_create_user(email: str, name: str = "") -> tuple[Any, bool]:
    """The account for ``email`` and whether this call created it.

    Addresses are matched case-insensitively and stored lower-case: the column is
    unique but case-sensitive, so ``Ada@x.test`` and ``ada@x.test`` would
    otherwise be two people. A created account has an unusable password and
    signs in through SSO, a password reset or an invitation.

    Raises :class:`InvalidEmailError` for a malformed address. Callers that go on
    to do more should wrap this in their own ``transaction.atomic``, so a later
    refusal does not leave the new account behind.
    """
    from django.core.exceptions import ValidationError
    from django.core.validators import validate_email
    from django.db import IntegrityError

    from apps.accounts.models import User

    email = (email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError as exc:
        raise InvalidEmailError("That is not a valid email address.") from exc
    user = User.objects.filter(email__iexact=email).first()
    if user is not None:
        return user, False
    try:
        with transaction.atomic():
            return User.objects.create_user(email=email, name=(name or "").strip()[:255]), True
    except IntegrityError:
        # A concurrent request may have created it between our read and insert.
        # Any other integrity failure is not ours to hide.
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            raise
        return user, False
