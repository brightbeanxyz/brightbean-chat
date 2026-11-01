"""``/api/platform/v1/`` — provisioning for the system that runs this installation.

Authenticated by a ``bbp_`` platform key (:class:`apps.api.auth.PlatformKeyAuth`),
which names no workspace: the organization and workspace come from the path, and
every nested id is looked up *through* its parent, so a workspace or key of
another organization is a 404 rather than a quiet cross-tenant write.

Routes read and delegate. Every write is a service call, because that is where
plan limits, the one-organization-per-account rule and uniqueness live — the
same rule ``test_facade_discipline.py`` holds the other routers to.
"""

from typing import Any
from uuid import UUID

from django.http import Http404, HttpRequest
from ninja import Router

from apps.accounts.services import InvalidEmailError
from apps.api.errors import ApiError
from apps.api.models import ApiKey
from apps.api.schemas import (
    ApiKeyIssue,
    ApiKeyIssued,
    OrganizationCreate,
    OrganizationOut,
    UserAdd,
    UserAdded,
    WorkspaceCreate,
    WorkspaceOut,
)
from apps.api.services import ApiKeysError, issue_api_key_as_system, revoke_api_key
from apps.members.models import OrgMembership
from apps.members.services import MembershipError
from apps.organizations.models import Organization
from apps.organizations.services import (
    OrganizationError,
    WorkspaceError,
    create_organization,
    create_workspace,
)
from apps.organizations.services import (
    add_user as add_org_user,
)
from apps.workspaces.models import Workspace

router = Router(tags=["platform"])


def _refuse(exc: Exception) -> ApiError:
    """Any coded refusal (duplicate, plan_*, other_organization, …) is a 409; uncoded is a 422."""
    code = str(getattr(exc, "code", "invalid"))
    if code != "invalid":
        return ApiError(str(exc), code=code, status=409)
    return ApiError(str(exc), code="invalid_request", status=422)


def _org(org_id: UUID) -> Organization:
    org = Organization.objects.filter(pk=org_id, deleted_at__isnull=True).first()
    if org is None:
        raise Http404("No such organization.")
    return org


def _workspace(org: Organization, ws_id: UUID, *, archived_ok: bool = False) -> Workspace:
    workspaces = Workspace.objects.for_org(org.pk)
    workspace = (workspaces if archived_ok else workspaces.filter(is_archived=False)).filter(pk=ws_id).first()
    if workspace is None:
        raise Http404("No such workspace.")
    return workspace


def _org_payload(org: Organization) -> dict[str, object]:
    return {
        "id": org.pk,
        "name": org.name,
        "external_id": org.external_id,
        "workspaces": [
            {"id": w.pk, "name": w.name, "is_archived": w.is_archived}
            for w in Workspace.objects.for_org(org.pk).order_by("name")
        ],
        "member_count": OrgMembership.objects.filter(organization=org).count(),
    }


@router.post("/organizations", response={200: OrganizationOut, 201: OrganizationOut}, url_name="organizations_create")
def create_org(request: HttpRequest, body: OrganizationCreate) -> Any:
    """Create a tenant: organization, owner account and first workspace.

    Repeating a request with the same ``external_id`` returns the existing
    organization with 200.
    """
    try:
        org, created = create_organization(
            name=body.name,
            owner_email=body.owner_email,
            owner_name=body.owner_name,
            workspace_name=body.workspace_name,
            external_id=body.external_id,
        )
    except InvalidEmailError as exc:
        raise ApiError("owner_email is not a valid address.", code="invalid_request", status=422) from exc
    except OrganizationError as exc:
        raise _refuse(exc) from exc
    return (201 if created else 200), _org_payload(org)


@router.get("/organizations/{org_id}", response=OrganizationOut, url_name="organizations_detail")
def get_org(request: HttpRequest, org_id: UUID) -> Any:
    return _org_payload(_org(org_id))


@router.post("/organizations/{org_id}/workspaces", response={201: WorkspaceOut}, url_name="workspaces_create")
def create_ws(request: HttpRequest, org_id: UUID, body: WorkspaceCreate) -> Any:
    org = _org(org_id)
    try:
        workspace = create_workspace(org, body.name)
    except WorkspaceError as exc:
        raise _refuse(exc) from exc
    return 201, {"id": workspace.pk, "name": workspace.name, "is_archived": workspace.is_archived}


@router.post("/organizations/{org_id}/users", response={200: UserAdded, 201: UserAdded}, url_name="users_add")
def add_user(request: HttpRequest, org_id: UUID, body: UserAdd) -> Any:
    """Add an account to the organization directly, creating it if needed.

    There is no invitation to accept; the person signs in through SSO or a
    password reset. Repeating the request for an existing member returns them
    unchanged with 200.
    """
    org = _org(org_id)
    try:
        user, membership, user_created, member_created = add_org_user(
            org,
            email=body.email,
            name=body.name,
            org_role=body.org_role,
            workspace_assignments=[{"workspace_id": str(a.workspace_id), "role": a.role} for a in body.workspaces],
        )
    except InvalidEmailError as exc:
        raise ApiError("email is not a valid address.", code="invalid_request", status=422) from exc
    except MembershipError as exc:
        raise _refuse(exc) from exc
    payload = {
        "id": user.pk,
        "email": user.email,
        "name": user.name,
        "org_role": membership.org_role,
        "user_created": user_created,
    }
    return (201 if member_created else 200), payload


@router.post(
    "/organizations/{org_id}/workspaces/{ws_id}/api-keys",
    response={201: ApiKeyIssued},
    url_name="api_keys_issue",
)
def issue_key(request: HttpRequest, org_id: UUID, ws_id: UUID, body: ApiKeyIssue) -> Any:
    """Issue a workspace key (``bb_…``) for ``/api/v1/``. The token appears only in this response."""
    workspace = _workspace(_org(org_id), ws_id)
    try:
        key = issue_api_key_as_system(workspace=workspace, name=body.name, scopes=body.scopes)
    except ApiKeysError as exc:
        raise _refuse(exc) from exc
    return 201, {
        "id": key.pk,
        "name": key.name,
        "scopes": key.scopes,
        "lookup_prefix": key.lookup_prefix,
        "token": key.raw_token,
    }


@router.delete(
    "/organizations/{org_id}/workspaces/{ws_id}/api-keys/{key_id}",
    response={204: None},
    url_name="api_keys_revoke",
)
def revoke_key(request: HttpRequest, org_id: UUID, ws_id: UUID, key_id: UUID) -> Any:
    # Revoking a credential must work whatever state its workspace is in.
    workspace = _workspace(_org(org_id), ws_id, archived_ok=True)
    key = ApiKey.objects.for_workspace(workspace).filter(pk=key_id).first()
    if key is None:
        raise Http404("No such key.")
    revoke_api_key(key)
    return 204, None
