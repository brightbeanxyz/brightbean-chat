"""Authentication and authorization on ``/api/v1/``.

The two properties this file is about:

* **One failure, one answer.** A missing header, a malformed token, an unknown
  key, a revoked key and a key for an archived workspace all produce the same
  401 with the same body. A caller who can tell those apart has an oracle.
* **``@require_permission`` works unchanged.** SPEC §4.2 says
  ``effective_permissions`` is the only protocol, and this API's whole
  authorization story is a nine-line dataclass that exposes it.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.api.auth import SCOPE_PERMISSIONS, VirtualMembership, permissions_for_scopes
from apps.api.models import ApiKey
from apps.api.tests.conftest import bearer, make_key
from apps.members.roles import PERMISSION_KEYS

CONTACTS = "/api/v1/contacts"


@pytest.mark.django_db
class TestBearerResolution:
    def test_a_valid_key_authenticates(self, client, tenancy, api_key):
        response = client.get(CONTACTS, **bearer(api_key[1]))

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "Bearer",
            "Bearer not-a-key",
            "Basic bb_aaaaaaaa",
            "Bearer bb_" + "a" * 43 + "_00000000",
        ],
    )
    def test_every_bad_credential_gets_the_same_401(self, client, db, header):
        kwargs = {"secure": True}
        if header is not None:
            kwargs["HTTP_AUTHORIZATION"] = header

        response = client.get(CONTACTS, **kwargs)

        assert response.status_code == 401
        assert response.json() == {
            "error": {"code": "unauthenticated", "message": "A valid API key is required.", "detail": {}}
        }
        assert response["WWW-Authenticate"] == 'Bearer realm="BrightBean Chat API"'

    def test_a_revoked_key_stops_working_immediately(self, client, tenancy, api_key):
        key, plaintext = api_key
        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 200

        from apps.api.services import revoke_api_key

        revoke_api_key(key)

        # No cache in front of the column, so the very next request is refused.
        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 401

    def test_a_key_for_an_archived_workspace_is_refused(self, client, tenancy, api_key):
        tenancy.workspace.is_archived = True
        tenancy.workspace.save(update_fields=["is_archived"])

        assert client.get(CONTACTS, **bearer(api_key[1])).status_code == 401

    def test_a_prefix_collision_does_not_authenticate_the_wrong_key(self, client, tenancy, other_tenancy):
        """Two rows sharing a lookup prefix must not be interchangeable.

        The prefix is only an index hint; the digest compare is the decision.
        Forcing a collision proves the loop over candidates picks by secret and
        not by "first row with this prefix".
        """
        mine, my_plaintext = make_key(tenancy.workspace)
        theirs, _ = make_key(other_tenancy.workspace, name="theirs")
        ApiKey.objects.for_workspace(other_tenancy.workspace).filter(pk=theirs.pk).update(
            lookup_prefix=mine.lookup_prefix
        )

        response = client.get(CONTACTS, **bearer(my_plaintext))

        assert response.status_code == 200
        # And it resolved to *my* workspace, not the colliding one.
        assert response.json()["data"] == []

    def test_a_bearer_over_plain_http_is_refused(self, client, tenancy, api_key):
        """SECURITY-BASELINE §5: a plaintext bearer has already leaked.

        ``DEBUG`` is the development escape hatch; the test settings are not in
        DEBUG, so the plain request here is the production behaviour.
        """
        response = client.get(CONTACTS, HTTP_AUTHORIZATION=f"Bearer {api_key[1]}")

        assert response.status_code == 401

    @override_settings(DEBUG=True)
    def test_plain_http_is_allowed_in_development(self, client, tenancy, api_key):
        response = client.get(CONTACTS, HTTP_AUTHORIZATION=f"Bearer {api_key[1]}")

        assert response.status_code == 200


class TestScopeMapping:
    def test_every_permission_key_gets_an_answer(self):
        """A partial mapping is a mapping whose meaning depends on the reader."""
        resolved = permissions_for_scopes(["read"])

        assert set(resolved) == set(PERMISSION_KEYS)

    def test_write_is_a_superset_of_read(self):
        assert SCOPE_PERMISSIONS["read"] <= SCOPE_PERMISSIONS["write"]

    @pytest.mark.parametrize(
        "forbidden",
        ["manage_api_keys", "manage_channels", "manage_members", "manage_workspace_settings", "manage_media"],
    )
    def test_no_scope_grants_management_permissions(self, forbidden):
        """A key that can mint keys is a key that never really gets revoked."""
        for scope in SCOPE_PERMISSIONS:
            assert forbidden not in SCOPE_PERMISSIONS[scope]

    def test_an_unknown_scope_grants_nothing(self):
        """A row ahead of the code denies rather than guessing permissively."""
        resolved = permissions_for_scopes(["read", "superuser"])

        assert resolved["use_inbox"] is True
        assert not any(resolved[key] for key in resolved if key not in SCOPE_PERMISSIONS["read"])

    def test_the_shim_exposes_only_effective_permissions(self):
        """SPEC §4.2's protocol, and nothing else.

        A shim that grew a ``user`` or a ``save()`` would be a shim that starts
        being passed where a real membership is expected.
        """
        membership = VirtualMembership({"use_inbox": True})

        assert set(vars(membership)) == {"effective_permissions"}


@pytest.mark.django_db
class TestScopeEnforcement:
    """Per-endpoint scope enforcement — a read key cannot write."""

    WRITES = [
        ("post", "/api/v1/contacts", {"first_name": "A"}),
        ("post", "/api/v1/messages", {"contact_id": None, "connection_id": None, "body": {"text": "hi"}}),
    ]

    def test_a_read_key_can_read(self, client, tenancy):
        _, plaintext = make_key(tenancy.workspace, scopes=("read",))

        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 200
        assert client.get("/api/v1/tags", **bearer(plaintext)).status_code == 200
        assert client.get("/api/v1/flows", **bearer(plaintext)).status_code == 200
        assert client.get("/api/v1/fields", **bearer(plaintext)).status_code == 200

    def test_a_read_key_cannot_create_a_contact(self, client, tenancy):
        _, plaintext = make_key(tenancy.workspace, scopes=("read",))

        response = client.post(
            CONTACTS,
            data={"first_name": "Ada"},
            content_type="application/json",
            **bearer(plaintext),
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        # The refused permission is not echoed: the caller knows their scopes,
        # and the message is identical whichever gate refused.
        assert "manage_crm" not in response.content.decode()

    def test_a_scopeless_key_can_do_nothing(self, client, tenancy):
        _, plaintext = make_key(tenancy.workspace, scopes=())

        assert client.get(CONTACTS, **bearer(plaintext)).status_code == 403


@pytest.mark.django_db
class TestFailedAuthThrottle:
    def test_repeated_failures_lock_the_client_address_out(self, client, tenancy, api_key, settings):
        """Checked before the digest, so guessing costs the guesser, not us.

        The response shape does not change, so the throttle is invisible to the
        thing it is throttling.
        """
        settings.API_AUTH_FAILURE_LIMIT = 3
        bad = "Bearer bb_" + "a" * 43 + "_00000000"

        for _ in range(5):
            assert client.get(CONTACTS, HTTP_AUTHORIZATION=bad, secure=True).status_code == 401

        # A *valid* key from the same address is now refused too — with the same
        # body, so the caller cannot tell the throttle from a bad credential.
        response = client.get(CONTACTS, **bearer(api_key[1]))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
