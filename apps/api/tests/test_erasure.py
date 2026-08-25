"""``DELETE /api/v1/contacts/<id>`` — SPEC §19's erasure over the public API.

Three properties, and the middle one is the reason this endpoint has its own
scope. A key that can erase must be one an admin deliberately issued; a key
issued last month to sync contacts must not have gained the power at upgrade.
"""

import json
from typing import Any

import pytest

from apps.api.tests.conftest import bearer, make_key
from apps.contacts.models import Contact, ContactErasure, ErasureStatus
from apps.contacts.tests.test_erasure import seed
from apps.messaging.models import ContactChannelIdentity, Message
from tests.support import Tenancy

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded(tenancy: Tenancy) -> dict[str, Any]:
    return seed(tenancy.workspace, nonce="zqxapi", label="a", user=tenancy.owner)


def erase_url(contact: Any, *, confirm: str | None = "erase") -> str:
    suffix = f"?confirm={confirm}" if confirm is not None else ""
    return f"/api/v1/contacts/{contact.pk}{suffix}"


class TestScope:
    def test_a_write_key_cannot_erase(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        """The escalation this design exists to prevent.

        Every key in the field carries ``write``. Folding ``erase_contacts``
        into it would have handed irreversible erasure to all of them on the
        day this merged, with nothing on the keys page changing.
        """
        _, token = make_key(tenancy.workspace, scopes=("read", "write"))

        response = client.delete(erase_url(seeded["contact"]), **bearer(token))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        assert Contact.objects.unscoped().filter(pk=seeded["contact"].pk).exists()

    def test_a_read_key_cannot_erase(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        _, token = make_key(tenancy.workspace, scopes=("read",))

        assert client.delete(erase_url(seeded["contact"]), **bearer(token)).status_code == 403

    def test_an_erase_key_can(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        response = client.delete(erase_url(seeded["contact"]), **bearer(token))

        assert response.status_code == 204
        assert not Contact.objects.unscoped().filter(pk=seeded["contact"].pk).exists()

    def test_only_an_admin_can_grant_it(self, tenancy: Tenancy) -> None:
        """``_validated_scopes`` caps a requested scope against the issuer's own
        permissions, which is what makes a separate scope the whole control."""
        from apps.api.services import ApiKeysError, issue_api_key

        with pytest.raises(ApiKeysError):
            issue_api_key(
                workspace=tenancy.workspace,
                issuer=tenancy.user_for("editor"),
                name="editor key",
                scopes=["erase"],
            )


class TestConfirmation:
    def test_a_delete_without_confirm_is_refused(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        """Not a permission check — ``erase_contacts`` is. This stops a cleanup
        script pointed at the wrong environment."""
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        response = client.delete(erase_url(seeded["contact"], confirm=None), **bearer(token))

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "confirmation_required"
        assert Contact.objects.unscoped().filter(pk=seeded["contact"].pk).exists()

    def test_the_wrong_confirm_value_is_refused(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        response = client.delete(erase_url(seeded["contact"], confirm="yes"), **bearer(token))

        assert response.status_code == 422

    def test_tenancy_is_checked_before_the_parameter(
        self, client: Any, tenancy: Tenancy, other_tenancy: Tenancy
    ) -> None:
        """CONTRIBUTING's ordering rule: a caller probing another workspace's
        ids gets the same bare 404 whether or not they remembered ``confirm``."""
        theirs = seed(other_tenancy.workspace, nonce="zqxother", label="o", user=other_tenancy.owner)
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        response = client.delete(erase_url(theirs["contact"], confirm=None), **bearer(token))

        assert response.status_code == 404


class TestItActuallyErases:
    def test_identities_and_messages_go(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        client.delete(erase_url(seeded["contact"]), **bearer(token))

        assert not ContactChannelIdentity.objects.unscoped().filter(pk=seeded["identity"].pk).exists()
        assert not Message.objects.unscoped().filter(conversation=seeded["conversation"]).exists()

    def test_a_second_call_404s(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        _, token = make_key(tenancy.workspace, scopes=("erase",))
        target = erase_url(seeded["contact"])
        client.delete(target, **bearer(token))

        assert client.delete(target, **bearer(token)).status_code == 404

    def test_it_records_which_key_did_it(self, client: Any, tenancy: Tenancy, seeded: dict[str, Any]) -> None:
        key, token = make_key(tenancy.workspace, scopes=("erase",))

        client.delete(erase_url(seeded["contact"]), **bearer(token))

        record = ContactErasure.objects.for_workspace(tenancy.workspace).get()
        assert record.api_key_id == key.pk
        assert record.source == "api"
        assert record.requested_by_id is None


class TestTheQueuedAnswer:
    def test_a_large_contact_answers_202_with_a_receipt(
        self, client: Any, tenancy: Tenancy, seeded: dict[str, Any], settings: Any
    ) -> None:
        """A 202 that cannot be followed up is a promise with no receipt."""
        settings.CONTACT_ERASURE_SYNC_MAX_MESSAGES = 0
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        response = client.delete(erase_url(seeded["contact"]), **bearer(token))

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == ErasureStatus.PENDING
        assert body["contact_id"] == str(seeded["contact"].pk)

    def test_the_receipt_is_pollable(
        self, client: Any, tenancy: Tenancy, seeded: dict[str, Any], settings: Any
    ) -> None:
        settings.CONTACT_ERASURE_SYNC_MAX_MESSAGES = 0
        _, token = make_key(tenancy.workspace, scopes=("erase",))
        erasure_id = client.delete(erase_url(seeded["contact"]), **bearer(token)).json()["id"]

        response = client.get(f"/api/v1/erasures/{erasure_id}", **bearer(token))

        assert response.status_code == 200
        assert response.json()["status"] == ErasureStatus.PENDING

    def test_a_second_request_while_one_is_running_is_a_conflict(
        self, client: Any, tenancy: Tenancy, seeded: dict[str, Any], settings: Any
    ) -> None:
        settings.CONTACT_ERASURE_SYNC_MAX_MESSAGES = 0
        _, token = make_key(tenancy.workspace, scopes=("erase",))
        target = erase_url(seeded["contact"])
        client.delete(target, **bearer(token))

        response = client.delete(target, **bearer(token))

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "erasure_in_progress"


class TestTheReceiptCarriesNoPii:
    def test_the_payload_names_ids_and_counts_only(
        self, client: Any, tenancy: Tenancy, seeded: dict[str, Any], settings: Any
    ) -> None:
        settings.CONTACT_ERASURE_SYNC_MAX_MESSAGES = 0
        _, token = make_key(tenancy.workspace, scopes=("erase",))

        body = client.delete(erase_url(seeded["contact"]), **bearer(token)).json()

        assert "zqxapi" not in json.dumps(body)
