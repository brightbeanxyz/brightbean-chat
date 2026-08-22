"""Identity resolution and the cross-channel linking rules."""

from typing import Any

import pytest

from apps.common.platforms import Platform
from apps.contacts.models import Contact, ContactStatus
from apps.contacts.services import create_contact
from apps.messaging.identities import bounded_address, normalized_address_for, resolve_identity
from apps.messaging.models import ContactChannelIdentity
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


def sms(workspace: Any, suffix: str = "sms") -> Any:
    return make_connection(workspace, platform=Platform.SMS, suffix=suffix)


def email(workspace: Any, suffix: str = "email") -> Any:
    return make_connection(workspace, platform=Platform.EMAIL, suffix=suffix)


class TestBoundedAddress:
    def test_it_hashes_rather_than_truncates(self) -> None:
        """Truncation narrows an identity key without saying so: two ids that
        agree in their first 200 characters would silently become one person,
        which on this table means one person receiving another's conversation."""
        a = bounded_address("x" * 300 + "a")
        b = bounded_address("x" * 300 + "b")
        assert a != b
        assert a.startswith("sha256:")
        assert len(a) <= 200

    def test_it_scrubs_nul_and_refuses_the_empty(self) -> None:
        assert bounded_address("\x00\x00") == ""
        assert bounded_address("  ") == ""
        assert bounded_address("u\x001") == "u1"


class TestExistingIdentity:
    def test_a_known_platform_user_wins_outright(self, tenancy: Any, connection: Any) -> None:
        """(connection, platform_user_id) is unique, so a hit is not a guess —
        the platform already told us whose thread this is."""
        first = resolve_identity(connection, "u1")
        second = resolve_identity(connection, "u1")
        assert second.identity.pk == first.identity.pk
        assert second.created_identity is False
        assert ContactChannelIdentity.objects.for_workspace(tenancy.workspace).count() == 1


class TestOpaquePlatformsNeverLink:
    @pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.INSTAGRAM, Platform.MESSENGER])
    def test_an_opaque_id_is_never_matched_against_a_contact(self, tenancy: Any, platform: str) -> None:
        """A Telegram chat id or a Meta PSID is an opaque per-app number. Matching
        on it would be matching on coincidence."""
        create_contact(tenancy.workspace, phone="+15550101234", email="a@b.com")
        connection = make_connection(tenancy.workspace, platform=platform, suffix=f"o-{platform}")
        resolution = resolve_identity(connection, "+15550101234")
        assert resolution.created_contact is True
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 2

    def test_whatsapp_is_deliberately_excluded(self) -> None:
        """Its wa_id *is* an E.164 number without the +, which would make it
        linkable — but the code that knows that is the adapter's, and the
        adapter is L5-C's."""
        assert Platform.WHATSAPP not in normalized_address_for(Platform.WHATSAPP, "15550101234")[0]


class TestLinkingBySms:
    def test_it_links_to_a_contact_holding_the_same_e164_number(self, tenancy: Any) -> None:
        existing = create_contact(tenancy.workspace, first_name="Ada", phone="+15550101234")
        resolution = resolve_identity(sms(tenancy.workspace), "+1 (555) 010-1234")
        assert resolution.contact.pk == existing.pk
        assert resolution.created_contact is False

    def test_an_unnormalisable_number_creates_a_contact_rather_than_guessing(self, tenancy: Any) -> None:
        """normalize_phone refuses to invent a country code, so a bare national
        number simply does not match. Failing to link leaves two rows, fixable
        with merge_contacts; linking wrongly staples two strangers' histories
        together and nothing can tell afterwards which messages were whose."""
        create_contact(tenancy.workspace, first_name="Ada", phone="5550101234")
        resolution = resolve_identity(sms(tenancy.workspace), "5550101234")
        assert resolution.created_contact is True

    def test_an_ambiguous_match_creates_a_new_contact(self, tenancy: Any) -> None:
        """THE rule. Two or more matches is never guessed."""
        create_contact(tenancy.workspace, first_name="Ada", phone="+15550101234")
        create_contact(tenancy.workspace, first_name="Grace", phone="+15550101234")
        resolution = resolve_identity(sms(tenancy.workspace), "+15550101234")
        assert resolution.created_contact is True
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 3

    def test_a_soft_deleted_contact_is_never_linked_to(self, tenancy: Any) -> None:
        """Linking to a merge tombstone would pull a deleted person back into a
        send path."""
        gone = create_contact(tenancy.workspace, first_name="Ada", phone="+15550101234")
        Contact.all_objects.filter(pk=gone.pk).update(status=ContactStatus.DELETED)
        resolution = resolve_identity(sms(tenancy.workspace), "+15550101234")
        assert resolution.created_contact is True

    def test_a_new_contact_carries_the_address_so_the_next_channel_can_link_back(self, tenancy: Any) -> None:
        resolution = resolve_identity(sms(tenancy.workspace), "+15550101234")
        assert resolution.contact.phone == "+15550101234"

    def test_another_workspaces_contact_is_not_a_match(self, tenancy: Any, other_tenancy: Any) -> None:
        create_contact(other_tenancy.workspace, first_name="Rival", phone="+15550101234")
        resolution = resolve_identity(sms(tenancy.workspace), "+15550101234")
        assert resolution.created_contact is True


class TestLinkingByEmail:
    def test_it_links_case_insensitively(self, tenancy: Any) -> None:
        existing = create_contact(tenancy.workspace, first_name="Ada", email="Ada@Example.COM")
        resolution = resolve_identity(email(tenancy.workspace), "ADA@example.com")
        assert resolution.contact.pk == existing.pk

    def test_a_malformed_address_creates_a_contact(self, tenancy: Any) -> None:
        resolution = resolve_identity(email(tenancy.workspace), "not-an-address")
        assert resolution.created_contact is True
        assert resolution.contact.email == ""


class TestCrossChannel:
    def test_an_sms_contact_and_an_email_contact_stay_one_person(self, tenancy: Any) -> None:
        """The point of the whole module: one human, two channels, one contact."""
        person = create_contact(tenancy.workspace, first_name="Ada", phone="+15550101234", email="ada@example.com")
        by_sms = resolve_identity(sms(tenancy.workspace), "+15550101234")
        by_email = resolve_identity(email(tenancy.workspace), "ada@example.com")
        assert by_sms.contact.pk == person.pk
        assert by_email.contact.pk == person.pk
        assert ContactChannelIdentity.objects.for_workspace(tenancy.workspace).count() == 2
