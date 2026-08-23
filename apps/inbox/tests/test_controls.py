"""Assignment, state, the automation pause and stopping a running flow.

Every one of these mutations goes through a facade — ``messaging.services`` for
conversation state and the pause, ``flows.engine`` for an execution — and the
tests are written to fail if one ever stops doing so.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.contacts.models import Contact
from apps.flows.models import ExecutionStatus, FlowExecution
from apps.flows.services import create_flow, latest_version
from apps.messaging.models import Conversation, ConversationState
from apps.messaging.services import AGENT_AUTOMATION_PAUSE, pause_automation
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction

pytestmark = pytest.mark.django_db


class TestAssignment:
    def test_an_agent_can_assign_and_unassign(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """SPEC §4.2 gives `reply_in_inbox` to agent and above, which is the
        issue's "Agent+" — no new permission key was needed."""
        url = url_for("assign", conversation_id=conversation.pk)
        editor = tenancy.user_for("editor")

        agent_client.post(url, {"assignee": str(editor.pk)})
        conversation.refresh_from_db()
        assert conversation.assignee_id == editor.pk

        agent_client.post(url, {"assignee": ""})
        conversation.refresh_from_db()
        assert conversation.assignee_id is None

    def test_somebody_outside_the_workspace_cannot_be_assigned(
        self, agent_client: Any, url_for: Any, conversation: Conversation, other_tenancy: Any
    ) -> None:
        """Checked against this workspace's memberships rather than against "is
        a user": without it, any user id in the deployment could be written onto
        a tenant's conversation by anyone holding reply_in_inbox anywhere."""
        outsider = other_tenancy.user_for("admin")

        response = agent_client.post(url_for("assign", conversation_id=conversation.pk), {"assignee": str(outsider.pk)})

        assert response.status_code == 204
        assert "not a member of this workspace" in response.headers["HX-Trigger"]
        conversation.refresh_from_db()
        assert conversation.assignee_id is None

    def test_a_junk_assignee_is_refused_rather_than_a_500(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        response = agent_client.post(url_for("assign", conversation_id=conversation.pk), {"assignee": "not-a-uuid"})

        assert response.status_code == 204
        conversation.refresh_from_db()
        assert conversation.assignee_id is None

    def test_a_viewer_cannot_assign(self, viewer_client: Any, url_for: Any, conversation: Conversation) -> None:
        response = viewer_client.post(url_for("assign", conversation_id=conversation.pk), {"assignee": ""})

        assert response.status_code == 403


class TestState:
    def test_done_and_reopen_both_go_through_the_facade(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        url = url_for("state", conversation_id=conversation.pk)

        agent_client.post(url, {"state": "done"})
        conversation.refresh_from_db()
        assert conversation.state == ConversationState.DONE

        agent_client.post(url, {"state": "open"})
        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN

    def test_the_header_control_flips_after_the_state_changes(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The state form posts and gets a 204 back, so nothing re-renders it on
        its own. Without a refresh it kept its old label and its old hidden
        value, and the only transition available was the one just made — the
        thread could be marked done and never reopened without a page reload."""
        agent_client.post(url_for("state", conversation_id=conversation.pk), {"state": "done"})

        header = agent_client.get(url_for("header", conversation_id=conversation.pk)).content.decode()

        assert "Reopen" in header
        assert "Mark done" not in header
        assert 'value="open"' in header

    def test_the_header_refreshes_on_the_event_the_mutations_fire(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A markup pin: the endpoint is only half the fix if nothing asks it."""
        pane = agent_client.get(
            url_for("thread", conversation_id=conversation.pk), headers={"HX-Request": "true"}
        ).content.decode()

        assert 'id="inbox-thread-header"' in pane
        assert 'hx-trigger="inboxThreadChanged from:body"' in pane

    def test_reopening_does_not_create_a_second_thread(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """open_conversation is get-or-reopen, and the unique constraint on
        (contact, channel_connection) is what makes that safe to call again."""
        url = url_for("state", conversation_id=conversation.pk)
        agent_client.post(url, {"state": "done"})

        agent_client.post(url, {"state": "open"})

        assert Conversation.objects.for_workspace(conversation.workspace_id).count() == 1

    def test_an_unknown_state_changes_nothing(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        response = agent_client.post(url_for("state", conversation_id=conversation.pk), {"state": "archived"})

        assert response.status_code == 204
        conversation.refresh_from_db()
        assert conversation.state == ConversationState.OPEN


class TestThePause:
    def test_pausing_uses_the_same_constant_a_reply_does(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A manual pause meaning something different from the pause a reply
        applies would be a surprise nobody asked for, so both read
        AGENT_AUTOMATION_PAUSE."""
        before = timezone.now()

        agent_client.post(url_for("pause", conversation_id=conversation.pk), {"action": "pause"})

        conversation.refresh_from_db()
        assert conversation.automation_paused_until is not None
        assert conversation.automation_paused_until >= before + AGENT_AUTOMATION_PAUSE

    def test_resuming_clears_it(self, agent_client: Any, url_for: Any, conversation: Conversation) -> None:
        pause_automation(conversation, timezone.now() + AGENT_AUTOMATION_PAUSE)

        agent_client.post(url_for("pause", conversation_id=conversation.pk), {"action": "resume"})

        conversation.refresh_from_db()
        assert conversation.automation_paused_until is None

    def test_the_banner_shows_while_paused_and_not_afterwards(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        url = url_for("messages", conversation_id=conversation.pk)
        assert "ib-banner-paused" not in agent_client.get(url).content.decode()

        pause_automation(conversation, timezone.now() + AGENT_AUTOMATION_PAUSE)
        assert "ib-banner-paused" in agent_client.get(url).content.decode()

    def test_a_lapsed_pause_does_not_show_a_banner(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The column keeps its value after the pause expires — routing compares
        it to now — so the banner has to compare too rather than test for null."""
        pause_automation(conversation, timezone.now() - timedelta(minutes=1))

        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "ib-banner-paused" not in body

    def test_the_standalone_panel_knows_the_conversation_is_paused(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The panel's toggle used to get `is_paused` only because the thread
        merged the body's context over the top — so the endpoint every refresh
        after a send or a pause goes through offered "Pause automation" at a
        conversation that was already paused."""
        pause_automation(conversation, timezone.now() + AGENT_AUTOMATION_PAUSE)

        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert "Resume automation" in panel
        assert "Pause automation" not in panel

    def test_a_viewer_cannot_pause(self, viewer_client: Any, url_for: Any, conversation: Conversation) -> None:
        response = viewer_client.post(url_for("pause", conversation_id=conversation.pk), {"action": "pause"})

        assert response.status_code == 403
        conversation.refresh_from_db()
        assert conversation.automation_paused_until is None


class TestStoppingAutomation:
    def _execution(self, tenancy: Any, contact: Contact) -> FlowExecution:
        flow = create_flow(workspace=tenancy.workspace, name="Onboarding")
        version = latest_version(flow)
        assert version is not None
        return FlowExecution.objects.create(
            flow=flow,
            flow_version=version,
            contact=contact,
            status=ExecutionStatus.WAITING_REPLY,
            current_node_id="n1",
        )

    def test_it_expires_the_live_execution(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, contact: Contact
    ) -> None:
        execution = self._execution(tenancy, contact)

        response = agent_client.post(url_for("stop", conversation_id=conversation.pk))

        assert response.status_code == 204
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.EXPIRED

    def test_it_cancels_the_queue_rows_that_would_have_woken_it(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, contact: Contact
    ) -> None:
        """A superseded execution's followup timer would otherwise fire hours
        later; resume_execution's status check would catch it, but a cancelled
        row is one that never wakes a worker at all."""
        self._execution(tenancy, contact)
        action = ScheduledAction.objects.create(
            workspace=tenancy.workspace,
            type=ActionType.FOLLOWUP_TIMER,
            status=ActionStatus.PENDING,
            run_at=timezone.now() + timedelta(hours=2),
            contact_id=contact.pk,
            payload={},
        )

        agent_client.post(url_for("stop", conversation_id=conversation.pk))

        action.refresh_from_db()
        assert action.status == ActionStatus.CANCELLED

    def test_it_says_so_when_nothing_is_running(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        response = agent_client.post(url_for("stop", conversation_id=conversation.pk))

        assert response.status_code == 204
        assert "Nothing running" in response.headers["HX-Trigger"]

    def test_the_indicator_shows_what_is_running(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation, contact: Contact
    ) -> None:
        self._execution(tenancy, contact)

        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert "Onboarding" in panel
        assert "Stop automation" in panel

    def test_a_viewer_cannot_stop_automation(
        self, tenancy: Any, viewer_client: Any, url_for: Any, conversation: Conversation, contact: Contact
    ) -> None:
        execution = self._execution(tenancy, contact)

        response = viewer_client.post(url_for("stop", conversation_id=conversation.pk))

        assert response.status_code == 403
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY


class TestTheTagQuickEditor:
    def test_an_agent_can_apply_and_remove_an_existing_tag(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """`edit_contact_fields` rather than `manage_crm`: SPEC §4.2 already
        splits the tag *vocabulary* (admin/editor) from one contact's own values
        (agent and up), and this endpoint can only apply a tag that exists."""
        from apps.contacts.models import Tag

        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")
        url = url_for("tags", conversation_id=conversation.pk)

        agent_client.post(url, {"tag": str(tag.pk)})
        assert list(conversation.contact.tags.all()) == [tag]

        agent_client.post(url, {"tag": str(tag.pk), "action": "remove"})
        assert list(conversation.contact.tags.all()) == []

    def test_another_tenants_tag_is_a_404(
        self, agent_client: Any, url_for: Any, conversation: Conversation, other_tenancy: Any
    ) -> None:
        from apps.contacts.models import Tag

        theirs = Tag.objects.create(workspace=other_tenancy.workspace, name="theirs")

        response = agent_client.post(url_for("tags", conversation_id=conversation.pk), {"tag": str(theirs.pk)})

        assert response.status_code == 404
        assert list(conversation.contact.tags.all()) == []

    def test_the_pickers_placeholder_is_a_no_op_rather_than_an_error(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        response = agent_client.post(url_for("tags", conversation_id=conversation.pk), {"tag": ""})

        assert response.status_code == 204

    def test_a_viewer_cannot_tag(
        self, tenancy: Any, viewer_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        from apps.contacts.models import Tag

        tag = Tag.objects.create(workspace=tenancy.workspace, name="vip")

        response = viewer_client.post(url_for("tags", conversation_id=conversation.pk), {"tag": str(tag.pk)})

        assert response.status_code == 403
        assert list(conversation.contact.tags.all()) == []
