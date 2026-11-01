"""``/api/platform/v1/``: authentication, provisioning, and the walls between tenants."""

import json

import pytest
from django.core.management import call_command
from django.urls import get_resolver
from django.utils import timezone

from apps.accounts.models import User
from apps.api.models import ApiKey
from apps.api.tests.conftest import bearer, make_key, make_platform_key
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace
from tests.idor import _walk

BASE = "/api/platform/v1"


@pytest.fixture
def tight_plan(monkeypatch):
    """A plan with one workspace, one seat and no API access."""
    from dataclasses import replace

    from apps.billing import entitlements
    from apps.billing.plans import UNLIMITED

    limits = replace(UNLIMITED, workspaces=1, seats=1, api_access=False)
    monkeypatch.setattr(entitlements, "limits_for", lambda organization: limits)


def post(client, path, body, auth):
    return client.post(f"{BASE}{path}", data=json.dumps(body), content_type="application/json", **auth)


@pytest.fixture
def org(client, platform_auth):
    response = post(
        client,
        "/organizations",
        {"name": "Acme", "external_id": "zirek-1", "owner_email": "Boss@Example.test", "workspace_name": "Main"},
        platform_auth,
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.django_db
class TestAuthentication:
    def test_a_platform_key_authenticates(self, client, org):
        assert org["name"] == "Acme"

    @pytest.mark.parametrize("header", [None, "Bearer", "Bearer bbp_" + "a" * 43 + "_00000000"])
    def test_every_bad_credential_gets_the_same_401(self, client, db, header):
        kwargs = {"secure": True}
        if header is not None:
            kwargs["HTTP_AUTHORIZATION"] = header

        response = client.get(f"{BASE}/organizations/00000000-0000-0000-0000-000000000000", **kwargs)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    def test_a_workspace_key_does_not_open_the_platform_api(self, client, tenancy):
        _, token = make_key(tenancy.workspace)

        response = client.get(f"{BASE}/organizations/{tenancy.organization.pk}", **bearer(token))

        assert response.status_code == 401

    def test_a_platform_key_does_not_open_the_workspace_api(self, client, db):
        _, token = make_platform_key()

        assert client.get("/api/v1/contacts", **bearer(token)).status_code == 401

    def test_a_revoked_key_stops_working_immediately(self, client, db):
        key, token = make_platform_key()
        url = f"{BASE}/organizations/00000000-0000-0000-0000-000000000000"
        assert client.get(url, **bearer(token)).status_code == 404

        call_command("revoke_platform_key", str(key.pk))

        assert client.get(url, **bearer(token)).status_code == 401

    def test_plain_http_is_refused(self, client, db):
        _, token = make_platform_key()

        response = client.get(
            f"{BASE}/organizations/00000000-0000-0000-0000-000000000000", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        assert response.status_code == 401


@pytest.mark.django_db
class TestOrganizations:
    def test_it_provisions_org_owner_and_workspace(self, org):
        owner = User.objects.get(email="boss@example.test")
        organization = Organization.objects.get(pk=org["id"])

        assert organization.external_id == "zirek-1"
        assert OrgMembership.objects.get(user=owner).org_role == "owner"
        assert [w["name"] for w in org["workspaces"]] == ["Main"]
        assert WorkspaceMembership.objects.get(user=owner).workspace_role == "admin"
        assert not owner.has_usable_password()

    def test_a_retry_with_the_same_external_id_finds_it(self, client, org, platform_auth):
        response = post(
            client,
            "/organizations",
            {"name": "Acme", "external_id": "zirek-1", "owner_email": "boss@example.test"},
            platform_auth,
        )

        assert response.status_code == 200
        assert response.json()["id"] == org["id"]
        assert Organization.objects.filter(external_id="zirek-1").count() == 1
        assert User.objects.filter(email="boss@example.test").count() == 1

    def test_a_replay_for_a_deleted_organization_is_a_conflict(self, client, org, platform_auth):
        Organization.objects.filter(pk=org["id"]).update(deleted_at=timezone.now())

        response = post(
            client,
            "/organizations",
            {"name": "Acme", "external_id": "zirek-1", "owner_email": "x@example.test"},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "deleted"

    def test_losing_the_race_for_an_external_id_returns_the_winner(self, monkeypatch, db):
        """Two requests pass the lookup together; the second's insert collides. It
        must answer with the first's organization, not a 500."""
        from apps.accounts.services import ExternalIdInUseError
        from apps.organizations import services

        winner = Organization.objects.create(name="Acme", external_id="raced")

        def lose(*args, **kwargs):
            raise ExternalIdInUseError("raced")

        monkeypatch.setattr(services, "provision_organization_and_workspace", lose)
        monkeypatch.setattr(services, "_replay", lambda found, name, owner_email: found)
        real_filter = Organization.objects.filter
        calls = {"n": 0}

        def filter_once_empty(*args, **kwargs):
            calls["n"] += 1
            return real_filter(pk=None) if calls["n"] == 1 else real_filter(*args, **kwargs)

        monkeypatch.setattr(Organization.objects, "filter", filter_once_empty)

        org, created = services.create_organization(name="Acme", owner_email="o@example.test", external_id="raced")

        assert org == winner and created is False

    def test_a_replay_with_a_different_name_is_a_conflict(self, client, org, platform_auth):
        response = post(
            client,
            "/organizations",
            {"name": "Other", "external_id": "zirek-1", "owner_email": "x@example.test"},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "external_id_conflict"

    def test_a_replay_with_a_different_owner_is_a_conflict(self, client, org, platform_auth):
        response = post(
            client,
            "/organizations",
            {"name": "Acme", "external_id": "zirek-1", "owner_email": "someone-else@example.test"},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "external_id_conflict"

    def test_a_replay_matches_the_owner_case_insensitively(self, client, org, platform_auth):
        response = post(
            client,
            "/organizations",
            {"name": "Acme", "external_id": "zirek-1", "owner_email": "BOSS@Example.test"},
            platform_auth,
        )

        assert response.status_code == 200

    def test_a_lost_race_with_no_winner_is_a_conflict_not_a_500(self, monkeypatch, db):
        from django.db import IntegrityError

        from apps.organizations import services

        def boom(*args, **kwargs):
            raise IntegrityError("unrelated")

        monkeypatch.setattr(services, "provision_organization_and_workspace", boom)

        with pytest.raises(services.OrganizationError) as exc:
            services.create_organization(name="Acme", owner_email="o@example.test", external_id="nobody")

        assert exc.value.code == "conflict"

    def test_the_same_owner_cannot_found_a_second_tenant(self, client, org, platform_auth):
        response = post(
            client,
            "/organizations",
            {"name": "Second", "external_id": "zirek-2", "owner_email": "boss@example.test"},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "other_organization"
        assert Organization.objects.count() == 1

    def test_over_long_input_is_refused_not_truncated(self, client, platform_auth):
        response = post(client, "/organizations", {"name": "x" * 101, "owner_email": "a@example.test"}, platform_auth)

        assert response.status_code == 422
        assert not Organization.objects.exists()

    def test_an_owner_already_in_an_organization_is_a_conflict(self, client, tenancy, platform_auth):
        response = post(client, "/organizations", {"name": "Second", "owner_email": tenancy.owner.email}, platform_auth)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "other_organization"

    @pytest.mark.parametrize(
        "body", [{"name": " ", "owner_email": "a@example.test"}, {"name": "X", "owner_email": "nope"}]
    )
    def test_bad_input_is_a_422_and_leaves_nothing_behind(self, client, platform_auth, body):
        response = post(client, "/organizations", body, platform_auth)

        assert response.status_code == 422
        assert not Organization.objects.exists()
        assert not User.objects.exists()

    def test_unknown_fields_are_refused(self, client, platform_auth):
        body = {"name": "X", "owner_email": "a@example.test", "is_staff": True}

        assert post(client, "/organizations", body, platform_auth).status_code == 422


@pytest.mark.django_db
class TestWorkspacesAndUsers:
    def test_a_second_workspace_is_created(self, client, org, platform_auth):
        response = post(client, f"/organizations/{org['id']}/workspaces", {"name": "Second"}, platform_auth)

        assert response.status_code == 201
        assert response.json()["name"] == "Second"

    def test_single_workspace_mode_refuses_a_second_workspace(self, client, org, platform_auth, settings):
        settings.SINGLE_WORKSPACE_MODE = True

        response = post(client, f"/organizations/{org['id']}/workspaces", {"name": "Second"}, platform_auth)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "single_workspace"
        assert Workspace.objects.filter(organization_id=org["id"]).count() == 1

    def test_the_plan_workspace_limit_is_a_conflict(self, client, org, platform_auth, tight_plan):
        response = post(client, f"/organizations/{org['id']}/workspaces", {"name": "Second"}, platform_auth)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "plan_workspaces"

    def test_an_over_long_name_is_refused_not_truncated(self, client, org, platform_auth):
        response = post(client, f"/organizations/{org['id']}/workspaces", {"name": "x" * 101}, platform_auth)

        assert response.status_code == 422

    def test_a_duplicate_workspace_name_is_a_conflict(self, client, org, platform_auth):
        response = post(client, f"/organizations/{org['id']}/workspaces", {"name": "Main"}, platform_auth)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "duplicate"

    def test_a_user_is_created_and_added_with_roles(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]
        body = {
            "email": "Dev@Example.test",
            "name": "Dev",
            "org_role": "member",
            "workspaces": [{"workspace_id": ws, "role": "editor"}],
        }

        response = post(client, f"/organizations/{org['id']}/users", body, platform_auth)

        assert response.status_code == 201
        assert response.json()["user_created"] is True
        assert response.json()["email"] == "dev@example.test"
        assert WorkspaceMembership.objects.get(user__email="dev@example.test").workspace_role == "editor"

    @pytest.mark.parametrize(
        "body",
        [
            {"email": "late@example.test", "org_role": "owner"},
            {"email": "late@example.test", "org_role": "wizard"},
            {
                "email": "late@example.test",
                "org_role": "member",
                "workspaces": [{"workspace_id": "00000000-0000-0000-0000-000000000000", "role": "viewer"}],
            },
        ],
    )
    def test_a_refused_add_leaves_no_account_behind(self, client, org, platform_auth, body):
        response = post(client, f"/organizations/{org['id']}/users", body, platform_auth)

        assert response.status_code == 422
        assert not User.objects.filter(email="late@example.test").exists()

    def test_no_free_seat_leaves_no_account_behind(self, client, org, platform_auth, tight_plan):
        response = post(
            client,
            f"/organizations/{org['id']}/users",
            {"email": "late@example.test", "org_role": "member"},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "plan_seats"
        assert not User.objects.filter(email="late@example.test").exists()

    def test_a_bad_address_is_a_422(self, client, org, platform_auth):
        response = post(
            client, f"/organizations/{org['id']}/users", {"email": "nope", "org_role": "member"}, platform_auth
        )

        assert response.status_code == 422

    def test_adding_the_same_person_twice_is_a_200(self, client, org, platform_auth):
        body = {"email": "boss@example.test", "org_role": "admin"}

        response = post(client, f"/organizations/{org['id']}/users", body, platform_auth)

        assert response.status_code == 200
        assert response.json()["org_role"] == "owner"

    def test_an_account_in_another_organization_is_a_conflict(self, client, org, other_tenancy, platform_auth):
        body = {"email": other_tenancy.owner.email, "org_role": "member"}

        response = post(client, f"/organizations/{org['id']}/users", body, platform_auth)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "other_organization"

    def test_nobody_is_added_as_an_owner(self, client, org, platform_auth):
        response = post(
            client, f"/organizations/{org['id']}/users", {"email": "o@example.test", "org_role": "owner"}, platform_auth
        )

        assert response.status_code == 422


@pytest.mark.django_db
class TestApiKeys:
    def test_a_workspace_key_is_issued_once_and_works_on_the_public_api(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "zirek", "scopes": ["read"]},
            platform_auth,
        )

        assert response.status_code == 201
        issued = response.json()
        assert issued["token"].startswith("bb_")
        assert client.get("/api/v1/contacts", **bearer(issued["token"])).status_code == 200
        stored = ApiKey.objects.unscoped().get(pk=issued["id"])
        assert stored.created_by is None
        assert issued["token"] not in {stored.token_digest, stored.lookup_prefix}

    def test_a_plan_without_api_access_is_a_conflict(self, client, org, platform_auth, tight_plan):
        ws = org["workspaces"][0]["id"]

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "plan_api"

    def test_the_per_workspace_cap_is_a_conflict(self, client, org, platform_auth, monkeypatch):
        monkeypatch.setattr("apps.api.services.MAX_KEYS_PER_WORKSPACE", 0)
        ws = org["workspaces"][0]["id"]

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "key_limit"

    def test_an_unknown_scope_is_a_422(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["root"]},
            platform_auth,
        )

        assert response.status_code == 422

    def test_a_key_can_be_revoked(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]
        base = f"{BASE}/organizations/{org['id']}/workspaces/{ws}/api-keys"
        issued = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        ).json()

        deleted = client.delete(f"{base}/{issued['id']}", **platform_auth)

        assert deleted.status_code == 204
        assert client.get("/api/v1/contacts", **bearer(issued["token"])).status_code == 401


@pytest.mark.django_db
class TestIsolation:
    """A platform key is global, so the walls are in the path: every nested id is checked against its parent."""

    def test_a_workspace_of_another_org_is_a_404_under_this_org(self, client, org, other_tenancy, platform_auth):
        stranger = other_tenancy.workspace.pk

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{stranger}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        )

        assert response.status_code == 404
        assert not ApiKey.objects.unscoped().filter(workspace=other_tenancy.workspace).exists()

    def test_a_key_of_another_org_cannot_be_revoked_through_this_one(self, client, org, other_tenancy, platform_auth):
        ws = org["workspaces"][0]["id"]
        victim, token = make_key(other_tenancy.workspace)

        response = client.delete(
            f"{BASE}/organizations/{org['id']}/workspaces/{ws}/api-keys/{victim.pk}", **platform_auth
        )

        assert response.status_code == 404
        assert client.get("/api/v1/contacts", **bearer(token)).status_code == 200

    def test_the_framework_root_serves_nothing(self, client, db):
        assert client.get(f"{BASE}/", secure=True).status_code == 404

    def test_an_unknown_organization_is_a_404(self, client, platform_auth):
        assert (
            client.get(f"{BASE}/organizations/00000000-0000-0000-0000-000000000000", **platform_auth).status_code == 404
        )

    def test_a_key_in_an_archived_workspace_can_still_be_revoked(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]
        base = f"{BASE}/organizations/{org['id']}/workspaces/{ws}/api-keys"
        issued = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        ).json()
        Workspace.objects.filter(pk=ws).update(is_archived=True)

        assert client.delete(f"{base}/{issued['id']}", **platform_auth).status_code == 204

    def test_an_archived_workspace_gets_no_keys(self, client, org, platform_auth):
        ws = org["workspaces"][0]["id"]
        Workspace.objects.filter(pk=ws).update(is_archived=True)

        response = post(
            client,
            f"/organizations/{org['id']}/workspaces/{ws}/api-keys",
            {"name": "k", "scopes": ["read"]},
            platform_auth,
        )

        assert response.status_code == 404

    def test_every_platform_route_is_covered_by_this_file(self):
        """Nothing under the platform prefix escapes the tests above unnoticed.

        ``tests/idor.py``'s session sweep cannot see these routes (their kwargs
        are not registered tenant kwargs, and a session would 401 them anyway),
        so this is the list that stands in for it. A new route fails here until
        it is added to ``COVERED`` with a test.
        """
        names = {r.name for r in _walk(get_resolver(None), (), None) if r.name.startswith("platform_v1:")}
        covered = {
            "platform_v1:organizations_create",
            "platform_v1:organizations_detail",
            "platform_v1:workspaces_create",
            "platform_v1:users_add",
            "platform_v1:api_keys_issue",
            "platform_v1:api_keys_revoke",
            # Ninja's own root name; with no docs page nothing is served there.
            "platform_v1:api-root",
        }

        assert names == covered


def test_the_reference_documents_every_route():
    """The doc is hand-written, so pin it to the URLconf: a route without a
    section fails here instead of drifting."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[3] / "docs" / "api" / "platform-v1.md").read_text()
    expected = {
        "platform_v1:organizations_create": "`POST /api/platform/v1/organizations`",
        "platform_v1:organizations_detail": "`GET /api/platform/v1/organizations/{org_id}`",
        "platform_v1:workspaces_create": "`POST /api/platform/v1/organizations/{org_id}/workspaces`",
        "platform_v1:users_add": "`POST /api/platform/v1/organizations/{org_id}/users`",
        "platform_v1:api_keys_issue": "`POST /api/platform/v1/organizations/{org_id}/workspaces/{ws_id}/api-keys`",
        "platform_v1:api_keys_revoke": "`DELETE /api/platform/v1/organizations/{org_id}/workspaces/{ws_id}/api-keys/{key_id}`",
    }
    names = {
        r.name
        for r in _walk(get_resolver(None), (), None)
        if r.name.startswith("platform_v1:") and r.name != "platform_v1:api-root"
    }

    assert names == set(expected)
    for name, heading in expected.items():
        assert heading in doc, f"{name} is not documented as {heading}"
