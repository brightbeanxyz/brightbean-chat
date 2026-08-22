"""The send box: the facade, the pause, compliance refusals and retry.

The inbox mutates messaging state only through ``apps.messaging.services``
(ROADMAP contract 1), so most of what these tests assert is about the *seam*:
that the facade is what runs, that its automation pause is the one that applies,
and that a refusal comes back as something the operator can read.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import TextBlock
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.messaging.codes import Denial, describe
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
)
from apps.messaging.services import AGENT_AUTOMATION_PAUSE

pytestmark = pytest.mark.django_db


def _messages(conversation: Conversation) -> Any:
    return Message.objects.for_workspace(conversation.workspace_id).filter(conversation=conversation)


class TestAnAgentReply:
    def test_it_goes_out_through_the_facade(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(
                url_for("send", conversation_id=conversation.pk), {"body": "on my way", "token": ""}
            )

        assert response.status_code == 204
        message = _messages(conversation).get()
        assert message.direction == MessageDirection.OUT
        assert message.source == MessageSource.AGENT
        assert message.status == MessageStatus.SENT
        assert len(adapter.sends) == 1

    def test_it_pauses_automation_for_the_facades_own_constant(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """SPEC §14's thirty minutes, asserted against the exported constant
        rather than the number: the pause lives inside send_as_agent, and the
        inbox must not be a second place that decides how long it is."""
        before = timezone.now()

        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "hello"})

        conversation.refresh_from_db()
        assert conversation.automation_paused_until is not None
        assert conversation.automation_paused_until >= before + AGENT_AUTOMATION_PAUSE
        assert conversation.automation_paused_until <= timezone.now() + AGENT_AUTOMATION_PAUSE

    def test_an_empty_reply_is_refused_without_touching_the_thread(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "   "})

        assert response.status_code == 204
        assert "Nothing to send" in response.headers["HX-Trigger"]
        assert not _messages(conversation).exists()
        assert adapter.sends == []

    def test_a_reply_past_the_cap_is_refused_here_rather_than_at_the_adapter(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        from apps.inbox.views import MAX_REPLY_CHARS

        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(
                url_for("send", conversation_id=conversation.pk), {"body": "x" * (MAX_REPLY_CHARS + 1)}
            )

        assert response.status_code == 204
        assert adapter.sends == []
        assert not _messages(conversation).exists()

    def test_a_double_submit_sends_once(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """The compose box carries its idempotency token (SPEC §9.4), so the
        second click collapses on message_unique_conv_idem instead of sending a
        duplicate."""
        token = "0" * 32
        url = url_for("send", conversation_id=conversation.pk)

        with registered(Platform.TELEGRAM) as adapter:
            agent_client.post(url, {"body": "hello", "token": token})
            agent_client.post(url, {"body": "hello", "token": token})

        assert _messages(conversation).count() == 1
        assert len(adapter.sends) == 1

    def test_a_missing_token_still_sends(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """Refusing an agent's reply because a hidden field went astray would be
        a worse failure than the duplicate it prevents."""
        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "hello"})

        assert _messages(conversation).count() == 1


class TestComplianceRefusals:
    def test_a_refusal_is_explained_rather_than_silently_dropped(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        identity.opted_out_at = timezone.now()
        identity.save(update_fields=["opted_out_at", "updated_at"])

        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "hello"})

        assert response.status_code == 204
        assert describe(Denial.OPTED_OUT.value) in response.headers["HX-Trigger"]
        assert adapter.sends == []

    def test_the_failed_row_carries_the_explanation_into_the_thread(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """The facade never raises for a send outcome; it writes a failed row
        with a machine code, and the thread turns that into a sentence."""
        identity.opted_out_at = timezone.now()
        identity.save(update_fields=["opted_out_at", "updated_at"])

        with registered(Platform.TELEGRAM):
            agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "hello"})
        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        message = _messages(conversation).get()
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.OPTED_OUT.value
        assert describe(Denial.OPTED_OUT.value) in body
        # The stored value is a machine code and stays out of the page.
        assert "opted_out" not in body

    def test_a_contact_with_no_address_here_is_explained_before_composing(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """No identity fixture: the compliance pre-flight has to say so rather
        than offering a send box that cannot work."""
        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert describe(Denial.NO_IDENTITY.value) in body

    def test_the_notice_reflects_a_reopened_window(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """Telegram's policy has no messaging window, so an agent is always
        clear to reply — and the notice is derived from that policy row, not
        from a branch in the template."""
        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "ib-banner-blocked" not in body


class TestRetry:
    def _failed(self, conversation: Conversation) -> Message:
        return Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AGENT,
            status=MessageStatus.FAILED,
            error=Denial.OPTED_OUT.value,
            idempotency_key="inbox:reply:old",
            body={"blocks": [{"type": "text", "text": "please reply"}]},
        )

    def test_it_re_sends_the_same_content_and_re_runs_compliance(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        """The window reopening between the refusal and the retry is exactly the
        case a backoff-ladder retry would not have re-decided."""
        failed = self._failed(conversation)

        with registered(Platform.TELEGRAM) as adapter:
            response = agent_client.post(
                url_for("retry", conversation_id=conversation.pk, message_id=failed.pk), {"token": "1" * 32}
            )

        assert response.status_code == 204
        assert len(adapter.sends) == 1
        block = adapter.sends[0].blocks[0]
        assert isinstance(block, TextBlock)
        assert block.text == "please reply"
        assert _messages(conversation).filter(status=MessageStatus.SENT).count() == 1

    def test_a_double_click_retries_once(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        failed = self._failed(conversation)
        url = url_for("retry", conversation_id=conversation.pk, message_id=failed.pk)

        with registered(Platform.TELEGRAM) as adapter:
            agent_client.post(url, {"token": "1" * 32})
            agent_client.post(url, {"token": "1" * 32})

        assert len(adapter.sends) == 1

    def test_a_message_that_did_not_fail_cannot_be_retried(
        self, agent_client: Any, url_for: Any, conversation: Conversation, outbound: Any
    ) -> None:
        sent = outbound("already gone", status=MessageStatus.SENT)

        response = agent_client.post(url_for("retry", conversation_id=conversation.pk, message_id=sent.pk))

        assert response.status_code == 404

    def test_an_internal_note_cannot_be_retried(
        self, agent_client: Any, url_for: Any, conversation: Conversation, outbound: Any
    ) -> None:
        """There is nothing to retry: a note was never sent to anybody."""
        note = outbound("a note", status=MessageStatus.FAILED, internal=True)

        response = agent_client.post(url_for("retry", conversation_id=conversation.pk, message_id=note.pk))

        assert response.status_code == 404

    def test_an_inbound_message_cannot_be_retried(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        message = inbound("from them")
        message.status = MessageStatus.FAILED
        message.save(update_fields=["status", "updated_at"])

        response = agent_client.post(url_for("retry", conversation_id=conversation.pk, message_id=message.pk))

        assert response.status_code == 404

    def test_a_message_from_another_conversation_is_a_404(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The message id is scoped to the conversation in the URL as well as to
        the workspace: without the pairing, any failed row in the tenant could
        be resent into any thread."""
        from apps.contacts.models import Contact
        from apps.messaging.services import open_conversation
        from apps.messaging.tests.conftest import make_connection

        other = open_conversation(
            workspace=tenancy.workspace,
            contact=Contact.objects.create(workspace=tenancy.workspace, first_name="Bob"),
            connection=make_connection(tenancy.workspace, suffix="other"),
        )
        elsewhere = self._failed(other)

        response = agent_client.post(url_for("retry", conversation_id=conversation.pk, message_id=elsewhere.pk))

        assert response.status_code == 404


class TestTheHumanAgentPath:
    def test_an_agent_send_may_use_an_allowance_an_automated_one_cannot(
        self, tenancy: Any, agent_client: Any, url_for: Any, contact: Any
    ) -> None:
        """SPEC §22 hard-codes the HUMAN_AGENT tag and the policy table decides
        whether a platform offers it. Messenger does; the inbox does not know
        that, and the tag arrives on the wire because compliance put it there.
        """
        from apps.messaging.services import open_conversation
        from apps.messaging.tests.conftest import make_connection

        connection = make_connection(tenancy.workspace, platform=Platform.MESSENGER, suffix="fb")
        conversation = open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=connection,
            platform=Platform.MESSENGER,
            platform_user_id="psid-1",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="message_in",
            # Outside the 24-hour window, which is what makes the allowance the
            # only way this send goes out at all.
            last_inbound_at=timezone.now() - timedelta(days=3),
            window_expires_at=timezone.now() - timedelta(days=2),
        )

        with registered(Platform.MESSENGER) as adapter:
            agent_client.post(url_for("send", conversation_id=conversation.pk), {"body": "sorry for the wait"})

        assert len(adapter.sends) == 1
        assert adapter.sends[0].tag == "HUMAN_AGENT"
