"""The three tables' shape, scoping and derived columns."""

from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.common.platforms import Platform
from apps.common.scoping import UnscopedQueryError
from apps.contacts.errors import WorkspaceMismatchError
from apps.contacts.services import create_contact
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
)
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db


@pytest.fixture
def contact(tenancy: Any) -> Any:
    return create_contact(tenancy.workspace, first_name="Ada")


class TestScoping:
    """SECURITY-BASELINE §1: every queryset on tenant data is scoped, and the
    manager refuses to execute one that is not."""

    @pytest.mark.parametrize("model", [ContactChannelIdentity, Conversation, Message])
    def test_an_unscoped_query_refuses_to_execute(self, model: Any) -> None:
        with pytest.raises(UnscopedQueryError):
            list(model.objects.all())

    def test_one_tenants_rows_are_invisible_to_another(
        self, tenancy: Any, other_tenancy: Any, contact: Any, connection: Any
    ) -> None:
        Conversation.objects.create(contact=contact, channel_connection=connection)
        assert Conversation.objects.for_workspace(tenancy.workspace).count() == 1
        assert Conversation.objects.for_workspace(other_tenancy.workspace).count() == 0


class TestDerivedColumns:
    """A denormalisation is a chance for two columns to disagree about whose row
    this is, so every one of them is derived rather than passed in."""

    def test_an_identity_takes_its_workspace_from_the_contact(self, contact: Any, connection: Any) -> None:
        identity = ContactChannelIdentity.objects.create(
            contact=contact, channel_connection=connection, platform=connection.platform, platform_user_id="u1"
        )
        assert identity.workspace_id == contact.workspace_id

    def test_a_message_takes_workspace_and_connection_from_the_conversation(
        self, contact: Any, connection: Any
    ) -> None:
        conversation = Conversation.objects.create(contact=contact, channel_connection=connection)
        message = Message.objects.create(conversation=conversation, direction=MessageDirection.IN)
        assert message.workspace_id == conversation.workspace_id
        assert message.channel_connection_id == connection.pk

    def test_a_caller_cannot_override_the_derivation(self, contact: Any, connection: Any, other_tenancy: Any) -> None:
        """Passing someone else's workspace is not a way to plant a row there."""
        conversation = Conversation.objects.create(contact=contact, channel_connection=connection)
        message = Message.objects.create(
            conversation=conversation, direction=MessageDirection.IN, workspace=other_tenancy.workspace
        )
        assert message.workspace_id == contact.workspace_id

    def test_an_update_fields_save_still_derives(self, contact: Any, connection: Any, other_tenancy: Any) -> None:
        """update_fields is widened, so a partial save cannot leave the derived
        columns behind on a row that has drifted."""
        conversation = Conversation.objects.create(contact=contact, channel_connection=connection)
        message = Message.objects.create(conversation=conversation, direction=MessageDirection.IN)
        # Drift, planted straight into the database so save() is what has to fix it.
        Message.all_objects.filter(pk=message.pk).update(workspace=other_tenancy.workspace)
        message.status = MessageStatus.SENT
        message.save(update_fields=["status"])
        message.refresh_from_db()
        assert message.workspace_id == contact.workspace_id

    def test_an_empty_update_fields_stays_a_no_op(self, contact: Any, connection: Any) -> None:
        """Django reads a falsy ``update_fields`` as "save nothing" and returns
        before touching the database. Widening it would turn a documented no-op
        into a real UPDATE, so the widening only ever grows a non-empty set."""
        conversation = Conversation.objects.create(contact=contact, channel_connection=connection)
        message = Message.objects.create(conversation=conversation, direction=MessageDirection.IN)
        message.status = MessageStatus.SENT
        message.save(update_fields=[])
        message.refresh_from_db()
        assert message.status == MessageStatus.QUEUED

    def test_a_peer_from_another_workspace_is_refused(self, contact: Any, other_tenancy: Any) -> None:
        foreign = make_connection(other_tenancy.workspace, suffix="rival")
        with pytest.raises(WorkspaceMismatchError):
            Conversation.objects.create(contact=contact, channel_connection=foreign)


class TestIdentityConstraints:
    def test_one_identity_per_connection_and_platform_user(self, contact: Any, connection: Any) -> None:
        ContactChannelIdentity.objects.create(
            contact=contact, channel_connection=connection, platform=connection.platform, platform_user_id="u1"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ContactChannelIdentity.objects.create(
                contact=contact, channel_connection=connection, platform=connection.platform, platform_user_id="u1"
            )

    def test_a_pending_identity_needs_no_connection(self, contact: Any) -> None:
        """ROADMAP contract 1: an address captured before a connection exists is
        stored and upgraded lazily at first send."""
        identity = ContactChannelIdentity.objects.create(
            contact=contact, channel_connection=None, platform=Platform.SMS, platform_user_id="+15550101234"
        )
        assert identity.is_pending
        assert identity.workspace_id == contact.workspace_id

    def test_pending_identities_do_not_duplicate(self, contact: Any) -> None:
        """Postgres treats NULLs as distinct, so the SPEC §5 unique cannot see
        these rows — which is why the second constraint has to exist."""
        ContactChannelIdentity.objects.create(
            contact=contact, channel_connection=None, platform=Platform.SMS, platform_user_id="+15550101234"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ContactChannelIdentity.objects.create(
                contact=contact, channel_connection=None, platform=Platform.SMS, platform_user_id="+15550101234"
            )


class TestMessageConstraints:
    @pytest.fixture
    def conversation(self, contact: Any, connection: Any) -> Conversation:
        return Conversation.objects.create(contact=contact, channel_connection=connection)

    def test_an_idempotency_key_is_unique_within_a_conversation(self, conversation: Conversation) -> None:
        Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            idempotency_key="k1",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Message.objects.create(
                conversation=conversation,
                direction=MessageDirection.OUT,
                source=MessageSource.AUTOMATION,
                idempotency_key="k1",
            )

    def test_blank_idempotency_keys_do_not_collide(self, conversation: Conversation) -> None:
        """The constraint is partial: an internal note has no key, and two of
        them are not a duplicate send."""
        Message.objects.create(
            conversation=conversation, direction=MessageDirection.OUT, source=MessageSource.AUTOMATION, internal=True
        )
        Message.objects.create(
            conversation=conversation, direction=MessageDirection.OUT, source=MessageSource.AUTOMATION, internal=True
        )
        assert Message.objects.for_workspace(conversation.workspace_id).count() == 2

    def test_a_provider_message_id_is_unique_per_connection(self, conversation: Conversation) -> None:
        Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            provider_message_id="pm-1",
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Message.objects.create(
                conversation=conversation,
                direction=MessageDirection.OUT,
                source=MessageSource.AUTOMATION,
                provider_message_id="pm-1",
            )

    def test_two_connections_may_reuse_a_provider_id(self, contact: Any, conversation: Conversation) -> None:
        """Provider ids are only unique inside the provider, so the constraint
        has to be scoped per connection — which is why Message carries one."""
        other = make_connection(contact.workspace, platform=Platform.SMS, suffix="second")
        elsewhere = Conversation.objects.create(contact=contact, channel_connection=other)
        Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            provider_message_id="pm-1",
        )
        Message.objects.create(
            conversation=elsewhere,
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            provider_message_id="pm-1",
        )
        assert Message.objects.for_workspace(contact.workspace_id).count() == 2


class TestConversationConstraints:
    def test_one_conversation_per_contact_and_connection(self, contact: Any, connection: Any) -> None:
        Conversation.objects.create(contact=contact, channel_connection=connection)
        with pytest.raises(IntegrityError), transaction.atomic():
            Conversation.objects.create(contact=contact, channel_connection=connection)
