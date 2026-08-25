"""The subject access response (SPEC §19, issue #29).

The thing worth testing here is not that the document has keys — it is that the
two categories a regulator actually asks about are in it. **Consent**, because
"we had permission" is not a fact anybody can act on without when and how; and
**what the controller keeps anyway**, because an Article 15 answer that quietly
omits the suppression list is the wrong answer even though the omission is
convenient.
"""

import json
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.models import EmailSuppression, SuppressionReason
from apps.common.platforms import Platform
from apps.contacts import subject_export
from apps.contacts.models import ContactStatus
from apps.contacts.services import (
    add_tag,
    create_contact,
    create_custom_field,
    delete_contact,
    get_or_create_tag,
    set_field_value,
)
from apps.contacts.tests.test_erasure import seed
from apps.messaging.models import ContactChannelIdentity, OptInSource
from apps.messaging.tests.conftest import make_connection
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

ALL_ROLES = ["admin", "editor", "agent", "viewer"]


def url(tenancy: Tenancy, contact: Any) -> str:
    return f"/w/{tenancy.workspace.pk}/contacts/{contact.pk}/export.json"


@pytest.fixture
def seeded(tenancy: Tenancy) -> dict[str, Any]:
    return seed(tenancy.workspace, nonce="zqxexport", label="e", user=tenancy.owner)


class TestTheDocument:
    def test_it_names_its_own_shape(self, seeded: dict[str, Any]) -> None:
        """Versioned like an exported flow, so a consumer reading one in a year
        does not have to guess what it is looking at."""
        document = subject_export.build(seeded["contact"])

        assert document["schema"] == "brightbean.contact_export"
        assert document["version"] == 1

    def test_every_section_the_issue_names_is_present(self, seeded: dict[str, Any]) -> None:
        document = subject_export.build(seeded["contact"])

        for section in ("contact", "custom_fields", "tags", "identities", "conversations", "executions"):
            assert document[section], section
        assert document["enrollments"]
        assert document["broadcasts"]

    def test_the_profile_carries_the_contact_columns(self, seeded: dict[str, Any]) -> None:
        document = subject_export.build(seeded["contact"])

        assert document["contact"]["first_name"] == "Adazqxexport"
        assert document["contact"]["email"] == "zqxexport@example.test"

    def test_message_bodies_are_included(self, seeded: dict[str, Any]) -> None:
        """ "Message history" means the messages, not a count of them."""
        document = subject_export.build(seeded["contact"])

        bodies = json.dumps(document["conversations"])
        assert "inbound zqxexport" in bodies
        assert "outbound zqxexport" in bodies

    def test_collected_variables_are_included(self, seeded: dict[str, Any]) -> None:
        """``variables`` holds what a data_collection node gathered — the
        subject's own answers, typed by them. Omitting it would leave out the
        most personal thing in the record."""
        document = subject_export.build(seeded["contact"])

        assert document["executions"][0]["variables"] == {"answer": "typed-zqxexport"}


class TestConsentIsIncluded:
    """SPEC §11.8. The part a regulator asks about."""

    def test_the_three_consent_columns_are_present_by_name(self, seeded: dict[str, Any]) -> None:
        document = subject_export.build(seeded["contact"])

        identity = document["identities"][0]
        assert identity["opt_in"] is True
        assert identity["opt_in_at"] is not None
        assert identity["opt_in_source"] == OptInSource.MESSAGE_IN
        assert "opted_out_at" in identity

    def test_an_opt_out_is_reported(self, tenancy: Tenancy) -> None:
        contact = create_contact(tenancy.workspace, first_name="Grace", source="manual")
        connection = make_connection(tenancy.workspace, suffix="optout")
        when = timezone.now()
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=Platform.TELEGRAM.value,
            platform_user_id="tg-optout",
            opt_in=False,
            opted_out_at=when,
        )

        document = subject_export.build(contact)

        assert document["identities"][0]["opted_out_at"] == when.isoformat()

    def test_a_pending_identity_still_reports_its_consent(self, tenancy: Tenancy) -> None:
        """An address captured before the workspace connected that platform has
        no channel connection at all. It is still consent, and still theirs."""
        contact = create_contact(tenancy.workspace, first_name="Pending", source="manual")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=None,
            platform=Platform.SMS.value,
            platform_user_id="+15550002222",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source=OptInSource.DATA_COLLECTION,
        )

        document = subject_export.build(contact)

        assert document["identities"][0]["opt_in_source"] == OptInSource.DATA_COLLECTION
        assert document["identities"][0]["channel_connection"] == ""


class TestItDisclosesWhatItKeeps:
    def test_a_surviving_suppression_is_in_the_document(self, tenancy: Tenancy) -> None:
        contact = create_contact(tenancy.workspace, email="bounced@example.test", source="manual")
        EmailSuppression.objects.create(
            workspace=tenancy.workspace,
            address="bounced@example.test",
            reason=SuppressionReason.HARD_BOUNCE.value,
        )

        document = subject_export.build(contact)

        assert document["retained"]["email_suppressions"][0]["address"] == "bounced@example.test"
        assert "mailbox" in document["retained"]["note"]

    def test_it_names_the_categories_it_cannot_reach(self, seeded: dict[str, Any]) -> None:
        """Raw webhook logs and stored import files. Both are pruned on a timer
        and neither is keyed by contact, so the honest answer is to say so."""
        document = subject_export.build(seeded["contact"])

        categories = {entry["category"] for entry in document["not_included"]}
        assert categories == {"Raw webhook payloads", "CSV import files"}


class TestTruncation:
    def test_a_long_history_says_it_was_cut(self, seeded: dict[str, Any], settings: Any) -> None:
        """A document that looks complete and is not is worse than one that
        admits the cut."""
        settings.CONTACT_EXPORT_MAX_MESSAGES = 1

        document = subject_export.build(seeded["contact"])

        assert document["truncated"]["messages"] is True

    def test_a_short_history_does_not(self, seeded: dict[str, Any]) -> None:
        document = subject_export.build(seeded["contact"])

        assert document["truncated"]["messages"] is False


class TestTheEndpoint:
    @pytest.mark.parametrize("role", ["agent", "viewer"])
    def test_reading_the_crm_is_not_enough(
        self, tenancy: Tenancy, client_for: Any, seeded: dict[str, Any], role: str
    ) -> None:
        """``manage_crm``, matching the CSV export of the whole workspace: a
        Viewer is read-only, not read-and-take."""
        response = client_for(tenancy.user_for(role)).get(url(tenancy, seeded["contact"]))

        assert response.status_code == 403

    @pytest.mark.parametrize("role", ["admin", "editor"])
    def test_a_manager_gets_the_file(
        self, tenancy: Tenancy, client_for: Any, seeded: dict[str, Any], role: str
    ) -> None:
        response = client_for(tenancy.user_for(role)).get(url(tenancy, seeded["contact"]))

        assert response.status_code == 200
        assert response["Content-Type"].startswith("application/json")
        assert "attachment" in response["Content-Disposition"]
        assert response["Cache-Control"] == "no-store"

    def test_the_filename_names_the_id_not_the_person(
        self, tenancy: Tenancy, client_for: Any, seeded: dict[str, Any]
    ) -> None:
        """A filename is the part of a document read by people who were never
        meant to see the contents."""
        response = client_for(tenancy.owner).get(url(tenancy, seeded["contact"]))

        assert "zqxexport" not in response["Content-Disposition"]
        assert str(seeded["contact"].pk) in response["Content-Disposition"]

    def test_a_tombstone_can_still_be_exported(self, tenancy: Tenancy, client_for: Any, seeded: dict[str, Any]) -> None:
        """The whole point of ``_erasable_or_404``.

        A subject access request usually arrives *after* somebody pressed
        Delete, and the rest of this subtree 404s a soft-deleted contact.
        """
        delete_contact(seeded["contact"])

        response = client_for(tenancy.owner).get(url(tenancy, seeded["contact"]))

        assert response.status_code == 200
        assert response.json()["contact"]["status"] == ContactStatus.DELETED

    def test_another_tenant_gets_404(
        self, tenancy: Tenancy, other_tenancy: Tenancy, client_for: Any, seeded: dict[str, Any]
    ) -> None:
        response = client_for(other_tenancy.owner).get(url(tenancy, seeded["contact"]))

        assert response.status_code == 404

    def test_it_404s_once_the_contact_is_erased(
        self, tenancy: Tenancy, client_for: Any, seeded: dict[str, Any]
    ) -> None:
        """The issue's acceptance criterion, in as many words: delete → export
        404s."""
        from apps.contacts import erasure
        from apps.contacts.models import ErasureSource

        target = url(tenancy, seeded["contact"])
        erasure.begin(seeded["contact"], source=ErasureSource.UI, requested_by=tenancy.owner)

        assert client_for(tenancy.owner).get(target).status_code == 404


class TestHostileContent:
    def test_a_platform_supplied_name_round_trips_as_data(self, tenancy: Tenancy, client_for: Any) -> None:
        """SECURITY-BASELINE §2. A contact's name arrives from a stranger, and
        the export is JSON — so the answer is that it stays a JSON string
        rather than becoming markup, and that the response is never served as
        HTML."""
        hostile = "<script>alert(1)</script>"
        contact = create_contact(tenancy.workspace, first_name=hostile, source="inbound")
        tag, _ = get_or_create_tag(tenancy.workspace, "ok")
        add_tag(contact, tag)
        field = create_custom_field(tenancy.workspace, name="Note", field_type="text")
        set_field_value(contact, field, hostile)

        response = client_for(tenancy.owner).get(url(tenancy, contact))

        assert response["Content-Type"].startswith("application/json")
        assert response.json()["contact"]["first_name"] == hostile
        assert response.json()["custom_fields"][0]["value"] == hostile
