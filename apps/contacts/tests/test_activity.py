"""The CRM's read of apps.messaging and apps.flows, and its two mutations.

Both mutations are the interesting part, because both cross an app boundary that
ROADMAP contract 3 governs:

* opting an identity out must go through the messaging facade and must **not**
  add a second writer for ``opted_out_at`` — ``apps/messaging/tests/
  test_write_sites.py`` scans the source tree for exactly that, and this issue
  keeps it passing unchanged by delegating rather than moving the assignment;
* stopping automation must go through the engine, which expires the run *and*
  cancels the queue rows that would have resumed it. A view assigning
  ``execution.status`` would leave those armed.
"""

import pytest
from django.utils import timezone

from apps.contacts import activity, services
from apps.flows.models import ExecutionStatus, FlowExecution
from apps.messaging.models import ContactChannelIdentity
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/{suffix}"


@pytest.fixture
def contact(db, tenancy):
    return services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")


@pytest.fixture
def identity(db, contact):
    row = ContactChannelIdentity(
        contact=contact,
        platform="telegram",
        platform_user_id="12345",
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="message_in",
    )
    row.save()
    return row


@pytest.mark.django_db
class TestOptOut:
    def test_it_records_the_withdrawal(self, tenancy, client_for, contact, identity):
        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{contact.pk}/identities/{identity.pk}/opt-out/")
        )

        assert response.status_code == 204
        identity.refresh_from_db()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False

    def test_it_is_idempotent(self, tenancy, client_for, contact, identity):
        """The first refusal is the one that counts: re-stamping would move the
        audit's answer to "when did they withdraw consent?" forward every time
        somebody pressed the button again."""
        client = client_for(tenancy.owner)
        client.post(url(tenancy, f"contacts/{contact.pk}/identities/{identity.pk}/opt-out/"))
        identity.refresh_from_db()
        first = identity.opted_out_at

        client.post(url(tenancy, f"contacts/{contact.pk}/identities/{identity.pk}/opt-out/"))

        identity.refresh_from_db()
        assert identity.opted_out_at == first

    def test_it_does_not_overwrite_the_consent_audit(self, tenancy, client_for, contact, identity):
        """``opt_in_source`` records how permission was *obtained*. Replacing it
        with "manual" would destroy that record at the moment it stopped
        applying, which is the pair a regulator asks to see together."""
        client_for(tenancy.owner).post(url(tenancy, f"contacts/{contact.pk}/identities/{identity.pk}/opt-out/"))

        identity.refresh_from_db()
        assert identity.opt_in_source == "message_in"
        assert identity.opt_in_at is not None

    def test_there_is_no_route_that_undoes_it(self):
        """SPEC §19 puts opt-out at a chokepoint so it cannot be bypassed. A
        toggle an operator could flip back is a bypass with a friendlier label —
        re-consent has to come from the contact (L5-D's keyword hook)."""
        from apps.contacts import urls as contact_urls

        names = {pattern.name for pattern in contact_urls.urlpatterns}

        assert "identity_opt_out" in names
        assert not any("opt_in" in name or "resubscribe" in name for name in names)

    def test_the_facade_is_the_door_and_ingest_stays_the_only_writer(self, identity):
        """The delegation ROADMAP contract 3 depends on, asserted directly."""
        from apps.messaging import ingest
        from apps.messaging import services as messaging_services

        assert messaging_services.record_opt_out(identity) is True
        assert messaging_services.record_opt_out(identity) is False
        assert ingest.apply_opt_out.__module__ == "apps.messaging.ingest"

    def test_an_identity_belonging_to_another_contact_is_a_404(self, tenancy, client_for, contact, identity):
        """The URL nests the identity under its contact; a nested id that is not
        checked against its parent is the classic version of this bug."""
        other = services.create_contact(tenancy.workspace, first_name="Someone else")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{other.pk}/identities/{identity.pk}/opt-out/")
        )

        assert response.status_code == 404
        identity.refresh_from_db()
        assert identity.opted_out_at is None

    def test_a_viewer_may_not_opt_anyone_out(self, tenancy, client_for, contact, identity):
        response = client_for(tenancy.user_for("viewer")).post(
            url(tenancy, f"contacts/{contact.pk}/identities/{identity.pk}/opt-out/")
        )

        assert response.status_code == 403


@pytest.mark.django_db
class TestChannelPresentation:
    def test_a_window_in_the_future_reads_open_and_one_in_the_past_reads_closed(self, tenancy, contact):
        from datetime import timedelta

        row = ContactChannelIdentity(contact=contact, platform="telegram", platform_user_id="1")
        row.window_expires_at = timezone.now() + timedelta(hours=2)
        row.save()

        assert activity.identities_for(contact)[0].window_open is True

        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(pk=row.pk).update(
            window_expires_at=timezone.now() - timedelta(hours=2)
        )
        assert activity.identities_for(contact)[0].window_open is False

    def test_a_null_window_reads_as_closed(self, tenancy, contact):
        """NULL means "no window has ever been opened", which is the same reading
        apps.messaging.compliance takes."""
        row = ContactChannelIdentity(contact=contact, platform="telegram", platform_user_id="1")
        row.save()

        assert activity.identities_for(contact)[0].window_open is False

    def test_reachable_is_consent_given_and_not_withdrawn(self, tenancy, contact, identity):
        assert activity.identities_for(contact)[0].reachable is True

        activity.opt_out(identity)
        assert activity.identities_for(contact)[0].reachable is False

    def test_the_list_annotates_opt_in_without_a_query_per_row(self, tenancy, client_for, contact, identity):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client = client_for(tenancy.owner)

        def render_with(count: int) -> int:
            for index in range(count):
                extra = services.create_contact(tenancy.workspace, first_name=f"C{index}-{count}")
                row = ContactChannelIdentity(contact=extra, platform="telegram", platform_user_id=f"{count}-{index}")
                row.save()
            client.get(url(tenancy, "contacts/"))  # warm session and nav queries
            with CaptureQueriesContext(connection) as captured:
                client.get(url(tenancy, "contacts/"))
            return len(captured.captured_queries)

        assert render_with(5) == render_with(25)

    def test_two_identities_on_one_platform_show_one_icon(self, tenancy, contact):
        """A workspace can legitimately run two Telegram bots, and two identical
        icons in a table cell is noise rather than information."""
        for address in ("1", "2"):
            row = ContactChannelIdentity(contact=contact, platform="telegram", platform_user_id=address)
            row.save()

        found = activity.platforms_for([contact], tenancy.workspace)

        assert found[contact.pk] == ["telegram"]


@pytest.mark.django_db
class TestAutomation:
    def _running(self, tenancy, contact):
        from apps.flows.models import FlowVersion
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Onboarding")
        version = FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=flow).first()
        execution = FlowExecution(
            flow=flow, flow_version=version, contact=contact, status=ExecutionStatus.WAITING_REPLY
        )
        execution.save()
        return flow, execution

    def test_stopping_expires_the_run_and_disarms_its_timers(self, tenancy, client_for, contact):
        """A view assigning execution.status would leave these rows armed, so the
        run the operator believed they had stopped would wake on its next timer."""
        _flow, execution = self._running(tenancy, contact)
        timer = ScheduledAction.objects.create(
            workspace=tenancy.workspace,
            contact_id=contact.pk,
            run_at=timezone.now(),
            type=ActionType.FOLLOWUP_TIMER,
            status=ActionStatus.PENDING,
        )

        response = client_for(tenancy.owner).post(url(tenancy, f"contacts/{contact.pk}/stop-automation/"))

        assert response.status_code == 204
        execution.refresh_from_db()
        timer.refresh_from_db()
        assert execution.status == ExecutionStatus.EXPIRED
        assert timer.status == ActionStatus.CANCELLED

    def test_expiring_is_not_failing(self, tenancy, client_for, contact):
        """L7-A's counters read `failed` as a flow that needs fixing; somebody
        ending a run is not that."""
        _flow, execution = self._running(tenancy, contact)

        client_for(tenancy.owner).post(url(tenancy, f"contacts/{contact.pk}/stop-automation/"))

        execution.refresh_from_db()
        assert execution.status != ExecutionStatus.FAILED

    def test_stopping_nothing_says_so_rather_than_claiming_success(self, tenancy, client_for, contact):
        import json

        response = client_for(tenancy.owner).post(url(tenancy, f"contacts/{contact.pk}/stop-automation/"))

        assert json.loads(response.headers["HX-Trigger"])["showToast"]["title"] == "Nothing was running"

    def test_only_published_flows_are_offered(self, tenancy, contact):
        """start_flow raises for a draft, so a picker listing them would be a
        picker full of error messages."""
        from apps.flows.services import create_flow

        draft = create_flow(workspace=tenancy.workspace, name="Draft only")

        assert draft not in list(activity.startable_flows(tenancy.workspace))
        assert activity.startable_flow(tenancy.workspace, draft.pk) is None

    def test_starting_an_unpublished_flow_is_a_toast_not_a_500(self, tenancy, client_for, contact):
        import json

        from apps.flows.services import create_flow

        draft = create_flow(workspace=tenancy.workspace, name="Draft only")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{contact.pk}/start-flow/"), {"flow_id": str(draft.pk)}
        )

        assert response.status_code == 204
        assert json.loads(response.headers["HX-Trigger"])["showToast"]["tone"] == "error"

    def test_a_malformed_flow_id_does_not_500(self, tenancy, client_for, contact):
        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{contact.pk}/start-flow/"), {"flow_id": "not-a-uuid"}
        )

        assert response.status_code == 204

    def test_another_workspaces_flow_cannot_be_started(self, tenancy, other_tenancy, client_for, contact):
        from apps.flows.services import create_flow

        theirs = create_flow(workspace=other_tenancy.workspace, name="Theirs")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{contact.pk}/start-flow/"), {"flow_id": str(theirs.pk)}
        )

        assert response.status_code == 204
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("role", ("agent", "viewer"))
    def test_starting_and_stopping_need_manage_crm(self, tenancy, client_for, contact, role):
        client = client_for(tenancy.user_for(role))

        assert client.post(url(tenancy, f"contacts/{contact.pk}/start-flow/")).status_code == 403
        assert client.post(url(tenancy, f"contacts/{contact.pk}/stop-automation/")).status_code == 403


@pytest.mark.django_db
class TestRecentMessages:
    def test_it_reads_across_conversations_newest_first(self, tenancy, contact):
        from apps.channels.models import ChannelConnection
        from apps.messaging.models import Conversation, Message, MessageDirection

        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace, platform="telegram", display_name="Bot", external_id="bot-activity"
        )
        conversation = Conversation(contact=contact, channel_connection=connection)
        conversation.save()
        for index in range(3):
            Message(
                conversation=conversation,
                direction=MessageDirection.IN,
                body={"blocks": [{"type": "text", "text": f"message {index}"}]},
            ).save()

        previews = activity.recent_messages(contact)

        assert [row.text for row in previews] == ["message 2", "message 1", "message 0"]
        assert all(row.inbound for row in previews)

    def test_a_body_whose_blocks_are_not_a_list_does_not_crash(self, tenancy, contact):
        from apps.channels.models import ChannelConnection
        from apps.messaging.models import Conversation, Message, MessageDirection

        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace, platform="telegram", display_name="Bot", external_id="bot-activity-2"
        )
        conversation = Conversation(contact=contact, channel_connection=connection)
        conversation.save()
        Message(conversation=conversation, direction=MessageDirection.IN, body={"blocks": "nope"}).save()

        assert activity.recent_messages(contact)[0].text == ""


@pytest.mark.django_db
class TestStartingAFlow:
    def _published(self, tenancy):
        from apps.flows.services import create_flow, publish_flow

        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        return flow, publish_flow

    def test_a_published_flow_is_offered_and_starts(self, tenancy, client_for, contact):
        from apps.flows.models import FlowVersion
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        version = FlowVersion.objects.for_workspace(tenancy.workspace).filter(flow=flow).first()
        # Published directly rather than through publish_flow: this test is about
        # the picker and the start, not about graph validation.
        FlowVersion.objects.for_workspace(tenancy.workspace).filter(pk=version.pk).update(published=True)

        assert flow in list(activity.startable_flows(tenancy.workspace))

    def test_starting_records_who_did_it(self, tenancy, client_for, contact, monkeypatch):
        """SPEC §5's started_by. "manual" is already in StartedBy.KINDS and the
        actor's id rides along, so "who started this?" is answerable from the
        execution row rather than only from a log line."""
        captured = {}

        def fake_start(contact_arg, flow_arg, *, started_by, **kwargs):
            captured["started_by"] = started_by
            return type("E", (), {"pk": "x"})()

        monkeypatch.setattr("apps.flows.engine.start_flow", fake_start)
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        monkeypatch.setattr(activity, "startable_flow", lambda workspace, flow_id: flow)

        client_for(tenancy.owner).post(url(tenancy, f"contacts/{contact.pk}/start-flow/"), {"flow_id": str(flow.pk)})

        assert captured["started_by"].startswith("manual")
        assert str(tenancy.owner.pk) in captured["started_by"]
