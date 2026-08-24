"""Suppression, and the one property that decides where it lives.

``apps/contacts/imports.py`` never fabricates identities — "a spreadsheet column
is not consent" — and ``imports._match`` deliberately skips deleted contacts. So
a contact that goes away and comes back is a **brand-new** ``Contact`` row with
no identity of its own, and everything the old identity knew is out of reach of
the new one.

Two ways the identity itself goes, and the tests below cover the sharper one:

* ``delete_contact`` is a *soft* delete, so the identity row survives — but it
  belongs to a tombstone, and ``_identity_for`` looks identities up **by
  contact**, so the re-imported contact cannot see it and cannot be given one
  either (the ``(connection, address)`` unique constraint is already taken).
* A **hard** delete — issue #29's GDPR erasure, or a merge — takes the identity
  with it, and with it every trace of the opt-out.

Either way the mailbox is the only durable key, which is what this module tests
rather than asserts: the first test erases the identity outright and then tries
to send.
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection, EmailSuppression, SuppressionReason
from apps.channels.providers import email_backends
from apps.channels.providers.email import EmailAdapter
from apps.channels.suppression import is_suppressed, suppress, suppress_and_opt_out
from apps.common.platforms import Platform
from apps.contacts.models import Contact, ContactStatus
from apps.contacts.services import create_contact, delete_contact
from apps.messaging.models import ContactChannelIdentity
from tests.support import email_identity

pytestmark = pytest.mark.django_db

ADDRESS = "gone@example.test"


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    row = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    row.credentials = {  # type: ignore[assignment]
        "provider": "smtp",
        "host": "mail.test",
        "security": "none",
        "from_address": "hello@sender.test",
    }
    row.save()
    return row


def identity_for(workspace: Any, connection: ChannelConnection, address: str = ADDRESS) -> ContactChannelIdentity:
    """This module's default address, on the shared builder."""
    return email_identity(workspace, connection, address)


def outbound() -> OutboundMessage:
    return OutboundMessage(blocks=(TextBlock(text="<p>Hi</p>"),), subject="Hello")


class TestItSurvivesReImport:
    """The reason the list is keyed on the address and not on the identity."""

    def test_a_suppressed_address_survives_erasure_and_re_import(
        self, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        identity = identity_for(tenancy.workspace, connection)
        suppress_and_opt_out(identity, reason=SuppressionReason.HARD_BOUNCE.value, connection=connection)
        assert identity.opted_out_at is not None

        # Erasure: the contact and its identity are gone outright, which is what
        # a GDPR subject-access deletion (#29) does and what a merge can do. The
        # opt-out went with the row that held it.
        contact = identity.contact
        identity.delete()
        contact.delete()
        assert ContactChannelIdentity.objects.for_workspace(tenancy.workspace).count() == 0

        # The address comes back on a fresh contact, with consent recorded — a
        # re-imported list, or the person filling in a form again.
        reimported = create_contact(tenancy.workspace, source="import", email=ADDRESS)
        fresh = ContactChannelIdentity.objects.create(
            contact=reimported,
            channel_connection=connection,
            platform=Platform.EMAIL.value,
            platform_user_id=ADDRESS,
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="import",
        )
        assert fresh.opted_out_at is None  # nothing on the identity says otherwise

        # The mailbox is still suppressed, because the mailbox is what bounced.
        assert is_suppressed(tenancy.workspace, ADDRESS)
        monkeypatch.setattr(email_backends, "deliver", _must_not_send)
        result = EmailAdapter().send(connection, fresh, outbound())
        assert result.status == "failed"
        assert result.error == "opted_out"

    def test_a_soft_deleted_contact_cannot_take_the_address_with_it(
        self, tenancy: Any, connection: ChannelConnection
    ) -> None:
        """The other half of the same story, and why it is not enough on its own.

        ``delete_contact`` keeps the identity, so the opt-out survives — but it
        belongs to a tombstone, and ``_identity_for`` resolves identities by
        contact. The re-imported contact therefore has none, which is safe today
        for a reason that has nothing to do with the opt-out.
        """
        identity = identity_for(tenancy.workspace, connection)
        suppress_and_opt_out(identity, reason=SuppressionReason.HARD_BOUNCE.value, connection=connection)
        delete_contact(identity.contact)

        reimported = create_contact(tenancy.workspace, source="import", email=ADDRESS)
        assert Contact.objects.for_workspace(tenancy.workspace).filter(status=ContactStatus.ACTIVE).count() == 1
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(contact=reimported).exists()
        assert is_suppressed(tenancy.workspace, ADDRESS)

    def test_the_refused_send_heals_the_new_identity(
        self, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One refusal by the adapter, then the compliance chokepoint takes over.

        That matters beyond tidiness: ``opted_out_at`` is what
        ``compliance.annotate_eligibility`` reads, so a broadcast's preview count
        is only correct once the identity knows.
        """
        suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.COMPLAINT.value)
        identity = identity_for(tenancy.workspace, connection)
        assert identity.opted_out_at is None

        monkeypatch.setattr(email_backends, "deliver", _must_not_send)
        EmailAdapter().send(connection, identity, outbound())

        identity.refresh_from_db()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False

    def test_a_suppressed_address_in_another_workspace_is_unaffected(
        self, tenancy: Any, other_tenancy: Any, connection: ChannelConnection
    ) -> None:
        """One tenant's bounce is not evidence about another's mailbox.

        They send from different domains, so the same address can legitimately
        be deliverable for one and not the other.
        """
        suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.HARD_BOUNCE.value)
        assert is_suppressed(tenancy.workspace, ADDRESS)
        assert not is_suppressed(other_tenancy.workspace, ADDRESS)


class TestTheList:
    def test_the_address_is_normalised(self, tenancy: Any) -> None:
        suppress(tenancy.workspace, "  GONE@Example.TEST ", reason=SuppressionReason.HARD_BOUNCE.value)
        assert is_suppressed(tenancy.workspace, "gone@example.test")
        assert is_suppressed(tenancy.workspace, "GONE@EXAMPLE.TEST")

    @pytest.mark.parametrize("address", ["", "not an address", "a@b", "@example.test", "a b@c.test", "x" * 400])
    def test_something_that_is_not_an_address_is_not_recorded(self, tenancy: Any, address: str) -> None:
        """A bounce payload's `To` is whatever the provider echoed back."""
        assert suppress(tenancy.workspace, address, reason=SuppressionReason.HARD_BOUNCE.value) is None
        assert EmailSuppression.objects.for_workspace(tenancy.workspace).count() == 0

    def test_the_first_reason_is_the_one_that_is_kept(self, tenancy: Any) -> None:
        """A mailbox that hard-bounced did not stop having hard bounced."""
        suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.HARD_BOUNCE.value, detail="5.1.1")
        suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.UNSUBSCRIBE.value)

        rows = EmailSuppression.objects.for_workspace(tenancy.workspace).filter(address=ADDRESS)
        assert rows.count() == 1
        row = rows.get()
        assert row.reason == SuppressionReason.HARD_BOUNCE
        assert row.detail == "5.1.1"

    def test_an_over_long_detail_is_truncated_rather_than_refused(self, tenancy: Any) -> None:
        row = suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.HARD_BOUNCE.value, detail="x" * 5000)
        assert row is not None and len(row.detail) == 200

    def test_disconnecting_the_channel_keeps_the_list(self, tenancy: Any, connection: ChannelConnection) -> None:
        """SET_NULL, not CASCADE: the list outlives the channel that produced it."""
        suppress(tenancy.workspace, ADDRESS, reason=SuppressionReason.HARD_BOUNCE.value, connection=connection)
        connection.delete()

        row = EmailSuppression.objects.for_workspace(tenancy.workspace).get(address=ADDRESS)
        assert row.connection_id is None

    def test_an_unknown_address_is_not_suppressed(self, tenancy: Any) -> None:
        assert not is_suppressed(tenancy.workspace, "someone@else.test")


class TestTheAdapterGate:
    def test_a_clean_address_reaches_the_backend(
        self, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[Any] = []

        def record(conn: Any, env: Any) -> str:
            sent.append(env)
            return "msg-1"

        monkeypatch.setattr(email_backends, "deliver", record)

        identity = identity_for(tenancy.workspace, connection, "fine@example.test")
        result = EmailAdapter().send(connection, identity, outbound())

        assert result.status == "sent"
        assert result.provider_message_id == "msg-1"
        assert sent[0].to == "fine@example.test"

    def test_an_identity_with_no_address_is_refused(
        self, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(email_backends, "deliver", _must_not_send)

        class Blank:
            platform_user_id = ""
            opted_out_at = None

        assert EmailAdapter().send(connection, Blank(), outbound()).error == "no_address"

    def test_a_connection_with_no_from_address_is_refused(self, tenancy: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        bare = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Bare",
            external_id="bare.test",
        )
        bare.credentials = {"provider": "smtp", "host": "mail.test"}  # type: ignore[assignment]
        bare.save()
        monkeypatch.setattr(email_backends, "deliver", _must_not_send)

        identity = identity_for(tenancy.workspace, bare, "fine@example.test")
        assert EmailAdapter().send(bare, identity, outbound()).error == "no_from_address"

    def test_a_message_with_no_subject_of_its_own_gets_a_default(
        self, tenancy: Any, connection: ChannelConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An inbox reply has no subject field, and must still be sendable.

        Refusing these meant an agent could not reply in an email conversation
        at all — not a compliance rule, just a missing default.
        """
        sent: list[Any] = []

        def record(conn: Any, env: Any) -> str:
            sent.append(env)
            return "msg-1"

        monkeypatch.setattr(email_backends, "deliver", record)
        identity = identity_for(tenancy.workspace, connection, "fine@example.test")
        message = OutboundMessage(blocks=(TextBlock(text="Hi"),))

        assert EmailAdapter().send(connection, identity, message).status == "sent"
        assert sent[0].subject


def _must_not_send(*args: Any, **kwargs: Any) -> str:
    raise AssertionError("The adapter reached a backend for a send it should have refused.")
