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

from apps.broadcasts import audience, handlers, services
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

    def test_a_slot_value_is_rendered_per_contact_through_the_shared_renderer(
        self, tenancy, make_contacts, whatsapp_connection, adapter_for
    ):
        """An operator maps a slot to ``{{first_name}}`` and each contact gets theirs.

        Through ``apps.flows.rendering`` — the one shared, engine-free
        substitution (SECURITY-BASELINE §3) — so a value containing ``{% ... %}``
        is equally inert, and the adapter receives finished strings.
        """
        from apps.broadcasts.tests.conftest import EVERYONE
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="greeting",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hello {{1}}."}},
        )
        make_contacts(2, connection=whatsapp_connection, prefix="Ada")
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Greeting", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_template(broadcast, template, {"body.1": "{{first_name}}"})

        with adapter_for(whatsapp_connection.platform) as adapter:
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

            filled = sorted(dict(send.template_variables)["body.1"] for send in adapter.sends)
            assert filled == ["Ada0", "Ada1"]

        # And the thread shows what the contact actually read, not the raw slot.
        bodies = [message.body["blocks"][0]["text"] for message in Message.objects.for_workspace(tenancy.workspace)]
        assert sorted(bodies) == ["Hello Ada0.", "Hello Ada1."]

    def test_a_template_that_stopped_being_sendable_is_not_sent(
        self, tenancy, make_contacts, whatsapp_connection, adapter_for
    ):
        """Hours pass between scheduling and sending, and Meta can reject in between.

        ``whatsapp_templates.sendable`` is the re-check, and its docstring names
        the dangerous half: a template name is scoped to the WABA, so the wrong
        connection can send approved-looking words that are not the ones anybody
        reviewed.
        """
        from apps.broadcasts.models import RecipientStatus
        from apps.broadcasts.tests.conftest import EVERYONE
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="withdrawn",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hi"}},
        )
        make_contacts(1, connection=whatsapp_connection)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Withdrawn", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_template(broadcast, template, {})

        with adapter_for(whatsapp_connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            WhatsAppTemplate.objects.for_workspace(tenancy.workspace).filter(pk=template.pk).update(
                status=WhatsAppTemplateStatus.REJECTED
            )
            for action in actions:
                _send(action)

            assert adapter.sends == []

        assert broadcast.recipients.get().status == RecipientStatus.FAILED

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


@pytest.mark.django_db
class TestIdentityAgreement:
    """The send must go to the identity fanout counted, not a differently-chosen one.

    ``audience.iter_candidates`` ranks a contact's candidate identities
    eligible-first; a send-time lookup that ordered only by connection and id
    could pick a different row for a contact holding two addresses on one
    connection — and then refuse the send for a reason nothing actually changed,
    which is the preview/send drift the shared rule list exists to prevent.
    """

    def _second_identity(self, tenancy, contact, connection, *, opted_out):
        from apps.messaging.models import ContactChannelIdentity, OptInSource

        return ContactChannelIdentity.objects.create(
            workspace=tenancy.workspace,
            contact=contact,
            channel_connection=connection,
            platform=connection.platform,
            platform_user_id=f"second-{contact.pk}",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source=OptInSource.IMPORT,
            opted_out_at=timezone.now() if opted_out else None,
        )

    def test_a_contact_with_one_opted_out_address_is_still_reached_on_the_other(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        [contact] = make_contacts(1, connection=connection)
        # A second address on the same connection that has opted out. Whichever
        # of the two sorts first by id, the eligible one is the one that counts.
        self._second_identity(tenancy, contact, connection, opted_out=True)
        broadcast = make_broadcast(connection=connection)

        preview = audience.preview(broadcast)
        assert preview.eligible == 1

        with adapter_for(connection.platform) as adapter:
            for action in _fan_out(tenancy.workspace, broadcast):
                _send(action)

            assert len(adapter.sends) == 1, "the send picked the opted-out address"

        assert broadcast.recipients.get().status == RecipientStatus.SENT

    def test_an_identity_deleted_between_fanout_and_send_falls_back(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """The recorded identity wins only while it is still usable."""
        from apps.messaging.models import ContactChannelIdentity

        [contact] = make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            actions = _fan_out(tenancy.workspace, broadcast)
            recorded = broadcast.recipients.get().identity_id
            spare = self._second_identity(tenancy, contact, connection, opted_out=False)
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=recorded).delete()

            for action in actions:
                _send(action)

            assert len(adapter.sends) == 1

        assert broadcast.recipients.get().status == RecipientStatus.SENT
        assert spare.pk is not None


@pytest.mark.django_db
def test_a_connection_disabled_after_scheduling_stops_the_send(
    tenancy, make_contacts, make_broadcast, connection, adapter_for
):
    """Switching a channel off is the most explicit "stop using this" there is.

    Nothing downstream enforces it: the facade takes the connection object it is
    handed and ``adapter_for`` keys on the platform, so a disabled connection
    goes on sending with its stored credentials. The composer's own selector
    already refuses one; the send has to agree.
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus

    make_contacts(2, connection=connection)
    broadcast = make_broadcast(connection=connection)

    with adapter_for(connection.platform) as adapter:
        actions = _fan_out(tenancy.workspace, broadcast)
        ChannelConnection.objects.for_workspace(tenancy.workspace).filter(pk=connection.pk).update(
            status=ConnectionStatus.DISABLED
        )
        for action in actions:
            _send(action)

        assert adapter.sends == []

    counts = services.counters(broadcast)
    assert counts.sent == 0
    assert counts.skipped == 2
    assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped
