"""The three trigger tables and the invariants the database itself holds."""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.common.platforms import Platform
from apps.common.scoping import UnscopedQueryError
from apps.contacts.errors import WorkspaceMismatchError
from apps.flows.models import DefaultReplyState, HandledComment, RoutedEvent, Trigger, TriggerType
from apps.flows.tests.support import connection_for, contact_for, published_flow
from apps.flows.triggers.types import PLATFORMS_FOR_TYPE

from .support import graph, node

#: A verb that touches nothing — the workspace has no tag by this name — so a
#: publishable one-node flow needs no fixture and no messaging facade. Same
#: NOOP_ACTION the runner tests use.
NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def _flow(workspace, name="Onboarding"):
    return published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name=name)


@pytest.mark.django_db
class TestTrigger:
    def test_workspace_is_derived_from_the_flow(self, tenancy):
        flow = _flow(tenancy.workspace)
        trigger = Trigger(flow=flow, type=TriggerType.KEYWORD, config_json={"keywords": []})
        trigger.save()

        assert trigger.workspace_id == flow.workspace_id

    def test_a_connection_from_another_workspace_is_refused(self, tenancy, other_tenancy):
        flow = _flow(tenancy.workspace)
        theirs = connection_for(other_tenancy.workspace, external_id="bot-rival")

        with pytest.raises(WorkspaceMismatchError):
            Trigger(flow=flow, channel_connection=theirs, type=TriggerType.KEYWORD).save()

    def test_the_manager_refuses_an_unscoped_query(self, tenancy):
        _flow(tenancy.workspace)
        with pytest.raises(UnscopedQueryError):
            Trigger.objects.filter(type=TriggerType.KEYWORD).count()

    def test_priority_cannot_be_negative(self, tenancy):
        flow = _flow(tenancy.workspace)
        with pytest.raises(IntegrityError):
            Trigger(flow=flow, type=TriggerType.KEYWORD, priority=-1).save()

    def test_ordering_is_a_total_order(self, tenancy):
        """Priority alone ties, and a tie makes first-match-wins non-deterministic."""
        flow = _flow(tenancy.workspace)
        first = Trigger(flow=flow, type=TriggerType.KEYWORD, priority=0)
        first.save()
        second = Trigger(flow=flow, type=TriggerType.KEYWORD, priority=0)
        second.save()

        ordered = list(Trigger.objects.for_workspace(tenancy.workspace).all())
        assert [row.pk for row in ordered] == [first.pk, second.pk]

    def test_every_type_has_a_platform_row(self):
        assert set(PLATFORMS_FOR_TYPE) == set(TriggerType.values)

    def test_email_is_not_an_inbound_trigger_platform(self):
        """SPEC §6.7 makes email outbound-only, so a keyword there could never fire."""
        assert Platform.EMAIL not in PLATFORMS_FOR_TYPE[TriggerType.KEYWORD]
        assert Platform.TELEGRAM in PLATFORMS_FOR_TYPE[TriggerType.KEYWORD]

    def test_covers_all_connections_reads_the_null(self, tenancy):
        flow = _flow(tenancy.workspace)
        trigger = Trigger(flow=flow, type=TriggerType.KEYWORD)
        trigger.save()

        assert trigger.covers_all_connections is True


@pytest.mark.django_db
class TestHandledComment:
    def _row(self, tenancy, connection, **overrides):
        fields = {
            "workspace_id": connection.workspace_id,
            "channel_connection": connection,
            "comment_id": "c-1",
            "post_id": "p-1",
            "commenter_ref": "ig-1",
            "commented_at": timezone.now(),
        }
        fields.update(overrides)
        row = HandledComment(**fields)
        row.save()
        return row

    def test_the_same_comment_cannot_be_recorded_twice(self, tenancy):
        connection = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        self._row(tenancy, connection)

        with pytest.raises(IntegrityError):
            self._row(tenancy, connection)

    def test_one_commenter_gets_one_row_per_post(self, tenancy):
        connection = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        self._row(tenancy, connection, comment_id="c-1")

        with pytest.raises(IntegrityError):
            self._row(tenancy, connection, comment_id="c-2")

    def test_the_guard_is_off_when_the_trigger_says_so(self, tenancy):
        """SPEC §10 makes once_per_contact_per_post a setting, so rows written
        while it was off stay out of the partial index."""
        connection = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        self._row(tenancy, connection, comment_id="c-1", once_per_contact_per_post=False)
        self._row(tenancy, connection, comment_id="c-2", once_per_contact_per_post=False)

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 2

    def test_the_guard_key_is_the_commenter_not_the_contact(self, tenancy):
        """Ingest creates no contact for a comment, and NULLs are distinct in a
        unique index — so keying on contact would mean the guard never fires."""
        connection = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")
        row = self._row(tenancy, connection)

        assert row.contact_id is None
        with pytest.raises(IntegrityError):
            self._row(tenancy, connection, comment_id="c-2")


@pytest.mark.django_db
class TestDefaultReplyState:
    def test_one_row_per_contact_and_channel(self, tenancy):
        connection = connection_for(tenancy.workspace, external_id="bot-a")
        contact = contact_for(tenancy.workspace)
        DefaultReplyState(
            workspace_id=tenancy.workspace.pk,
            contact=contact,
            channel_connection=connection,
            last_sent_at=timezone.now(),
        ).save()

        with pytest.raises(IntegrityError):
            DefaultReplyState(
                workspace_id=tenancy.workspace.pk,
                contact=contact,
                channel_connection=connection,
                last_sent_at=timezone.now() - timedelta(days=2),
            ).save()


@pytest.mark.django_db
class TestRoutedEvent:
    def test_one_row_per_connection_and_event(self, tenancy):
        connection = connection_for(tenancy.workspace, external_id="bot-a")
        RoutedEvent(
            workspace_id=tenancy.workspace.pk,
            channel_connection=connection,
            provider_event_id="evt-1",
            stage="resume",
        ).save()

        with pytest.raises(IntegrityError):
            RoutedEvent(
                workspace_id=tenancy.workspace.pk,
                channel_connection=connection,
                provider_event_id="evt-1",
                stage="trigger",
            ).save()
