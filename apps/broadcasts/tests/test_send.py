"""The send half of SPEC §13.2 — one contact's copy, and everything re-checked.

The two properties worth stating in one place:

* **the send re-asks compliance**, so a contact who opts out between fanout and
  send is skipped and *counted* rather than messaged or silently dropped;
* **the send goes through contract 1's facade**, so it takes a token from the
  connection's bucket and there is no second throttle anywhere in this app.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.broadcasts import handlers, services
from apps.broadcasts.models import RecipientStatus
from apps.messaging.codes import Denial
from apps.messaging.models import ContactChannelIdentity, Message, MessageSource, MessageStatus
from apps.queueing.models import ActionType, ScheduledAction


def _fan_out(workspace, broadcast):
    """Schedule and expand, leaving one pending send action per eligible contact."""
    services.schedule_broadcast(broadcast)
    action = ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
    handlers.handle_broadcast_fanout(action.payload, action)
    broadcast.refresh_from_db()
    return list(
        ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_SEND).order_by("run_at")
    )


def _send(action):
    handlers.handle_broadcast_send(action.payload, action)


@pytest.mark.django_db
class TestMiniFlowContent:
    def test_it_sends_through_the_facade_and_reaches_the_adapter(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection, text="Hello from the broadcast")

        with adapter_for(connection.platform) as adapter:
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

            assert len(adapter.sends) == 2
            assert adapter.sends[0].blocks[0].text == "Hello from the broadcast"

        assert broadcast.recipients.filter(status=RecipientStatus.SENT).count() == 2

    def test_the_message_records_source_broadcast(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """SPEC §5's vocabulary, reached through the engine's send envelope.

        A run started by a broadcast sends as ``broadcast``, not ``automation``:
        ``apps.flows.engine.sending.envelope_for`` derives it from
        ``started_by``, which is the only thing that knows.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.source == MessageSource.BROADCAST

    def test_the_execution_is_stamped_with_the_broadcast(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """``started_by="broadcast:<id>"``, which is what makes buttons behave.

        A one-shot flow start means a waiting execution exists, so a postback
        from this message resumes it exactly as it would in any other flow.
        """
        from apps.flows.models import FlowExecution

        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

        execution = FlowExecution.objects.for_workspace(tenancy.workspace).get()
        assert execution.started_by == f"broadcast:{broadcast.pk}"
        assert execution.preview is False

    def test_the_message_is_attached_to_its_recipient_row(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """How delivered/read counters stay live with no receipt path of our own.

        ``apps.messaging.ingest`` advances ``Message.status`` when a receipt
        arrives; the counters read it back through this foreign key.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

        recipient = broadcast.recipients.get()
        assert recipient.message_id is not None
        assert recipient.message.status == MessageStatus.SENT

    def test_a_message_tag_reaches_the_wire(
        self, tenancy, make_contacts, make_broadcast, messenger_connection, adapter_for
    ):
        """SPEC §6.4's whole point, end to end.

        The audience is outside the window, so compliance would refuse an untagged
        send. The composer's tag travels on the execution and
        ``apps.flows.engine.sending`` puts it on the outbound message before
        ``can_send`` sees it — which is both what makes the send legal and what
        the platform requires on the payload.
        """
        make_contacts(2, connection=messenger_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=messenger_connection, tag="ACCOUNT_UPDATE")

        with adapter_for(messenger_connection.platform) as adapter:
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

            assert [send.tag for send in adapter.sends] == ["ACCOUNT_UPDATE", "ACCOUNT_UPDATE"]

        assert broadcast.recipients.filter(status=RecipientStatus.SENT).count() == 2


@pytest.mark.django_db
class TestTemplateContent:
    def test_a_template_broadcast_sends_directly_through_the_facade(
        self, tenancy, make_contacts, whatsapp_connection, adapter_for
    ):
        """Template content needs no execution: there are no buttons to wait on."""
        from apps.broadcasts.tests.conftest import EVERYONE
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus
        from apps.flows.models import FlowExecution

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="order_shipped",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Your order {{1}} shipped."}},
        )
        make_contacts(2, connection=whatsapp_connection, window=-timedelta(hours=1))
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Shipping", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_template(broadcast, template, {"body.1": "A-1"})

        with adapter_for(whatsapp_connection.platform) as adapter:
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

            assert len(adapter.sends) == 2
            assert adapter.sends[0].template_ref == template.reference
            assert adapter.sends[0].template_variables == (("body.1", "A-1"),)

        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()
        assert Message.objects.for_workspace(tenancy.workspace).first().source == MessageSource.BROADCAST

    def test_the_send_key_is_the_one_the_spec_fixes(self, tenancy, make_contacts, whatsapp_connection, adapter_for):
        """``broadcast:<id>:contact:<id>`` on the message as well as the action.

        Which is what makes a second delivery of the same send collapse onto the
        row that already exists rather than calling the provider twice.
        """
        from apps.broadcasts.tests.conftest import EVERYONE
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="hello_there",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hi"}},
        )
        [contact] = make_contacts(1, connection=whatsapp_connection)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Hi", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_template(broadcast, template, {})

        with adapter_for(whatsapp_connection.platform):
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.idempotency_key == f"broadcast:{broadcast.pk}:contact:{contact.pk}"


@pytest.mark.django_db
class TestSendTimeRecheck:
    def test_a_contact_who_opts_out_after_fanout_is_skipped_and_counted(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """The acceptance criterion, exactly.

        Hours can pass between fanout and send. The eligibility filter ran on the
        set; this re-asks ``can_send`` for the individual, which is SPEC §19's
        chokepoint doing its job at the last possible moment.
        """
        contacts = make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(contact=contacts[0]).update(
                opted_out_at=timezone.now()
            )

            for action in actions:
                _send(action)

            assert len(adapter.sends) == 2

        counts = services.counters(broadcast)
        assert counts.sent == 2
        assert counts.skipped == 1
        assert counts.skips == {Denial.OPTED_OUT.value: 1}
        assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped

    def test_a_contact_deleted_after_fanout_is_skipped(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        from apps.contacts.models import Contact, ContactStatus

        contacts = make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            Contact.objects.for_workspace(tenancy.workspace).filter(pk=contacts[0].pk).update(
                status=ContactStatus.DELETED
            )
            for action in actions:
                _send(action)

            assert len(adapter.sends) == 1


@pytest.mark.django_db
class TestRateLimiting:
    def test_the_send_takes_a_token_from_the_connections_bucket(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """A broadcast must not bypass the bucket, and must not add one beside it.

        The bucket row is created by the first send at full capacity and debited
        one token per message, which is ``apps.messaging.buckets`` doing exactly
        what it does for a flow send — no second counter is involved.
        """
        from apps.messaging.buckets import capacity_for, rate_for
        from apps.messaging.models import SendBucket

        make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

        bucket = SendBucket.objects.get(connection=connection)
        rate = rate_for(connection.platform)
        # The rate is the policy's, not a number this app chose — which is the
        # "must not add a second throttle beside it" half of the requirement.
        assert bucket.refill_rate == pytest.approx(rate)
        # And it was debited. Not "capacity minus three": the bucket refills by
        # elapsed time on every acquire, so an exact residue is a clock
        # assertion rather than a rate-limiting one.
        assert bucket.tokens < capacity_for(rate)

    def test_an_empty_bucket_defers_rather_than_dropping(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for, settings
    ):
        """SPEC §8's fallback, reached through the facade rather than reinvented.

        With no tokens the facade queues the message and arms a ``send_retry``;
        the recipient is still recorded as sent because it is on its way, and the
        counters follow the message row from there.
        """
        from apps.messaging.buckets import rate_for
        from apps.messaging.models import SendBucket

        settings.SEND_BUCKET_MAX_WAIT_SECONDS = 0
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        actions = _fan_out(tenancy.workspace, broadcast)
        SendBucket.objects.create(
            connection=connection,
            tokens=0.0,
            capacity=1.0,
            refill_rate=rate_for(connection.platform),
            refilled_at=timezone.now() + timedelta(hours=1),
        )

        with adapter_for(connection.platform) as adapter:
            for action in actions:
                _send(action)

            assert adapter.sends == []

        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.status == MessageStatus.QUEUED
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.SEND_RETRY).exists()


@pytest.mark.django_db
class TestReEntrancy:
    def test_running_a_send_twice_calls_the_provider_once(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """Zombie recovery can re-run a handler whose transaction committed.

        Two guards make that safe and both are exercised here: the recipient row
        has already left ``pending``, and the message's idempotency key owns the
        provider call regardless.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            for _ in range(3):
                for action in actions:
                    _send(action)

            assert len(adapter.sends) == 1

        assert Message.objects.for_workspace(tenancy.workspace).count() == 1
