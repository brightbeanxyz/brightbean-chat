"""Labels: on a thread, in the list filter, and in the settings manager (SPEC §14).

The permission split is the thing worth stating up front, because it is the one
decision here that is not obvious. Applying and creating a label is
``reply_in_inbox``: a label is the inbox's own filing system, and an Agent who
has to ask an Admin before they can file anything does not have an inbox. The
*rules* manager next door is ``manage_workspace_settings``, because a rule acts
on other people's conversations. Both keys already exist; nothing is invented.
"""

from typing import Any

import pytest

from apps.inbox import selectors, services
from apps.inbox.models import MAX_LABELS_PER_CONVERSATION, ConversationLabel, ConversationLabelLink
from apps.messaging.models import Conversation

pytestmark = pytest.mark.django_db


def _label(workspace: Any, name: str = "Refunds", color: str = "#3B82F6") -> ConversationLabel:
    return services.create_label(workspace, name=name, color=color)


def _names(conversation: Conversation) -> list[str]:
    return sorted(
        link.label.name
        for link in ConversationLabelLink.objects.for_workspace(conversation.workspace_id)
        .filter(conversation=conversation)
        .select_related("label")
    )


class TestThePalette:
    def test_names_are_unique_case_insensitively(self, tenancy):
        """A `Lower(name)` expression constraint, like contacts.Tag — and
        `full_clean` validates those, which is what turns the clash into a
        sentence a toast can carry instead of an IntegrityError."""
        _label(tenancy.workspace, "Refunds")

        with pytest.raises(services.InboxError):
            _label(tenancy.workspace, "refunds")

    def test_two_workspaces_may_share_a_name(self, tenancy, other_tenancy):
        _label(tenancy.workspace, "Refunds")
        _label(other_tenancy.workspace, "Refunds")

        assert ConversationLabel.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_bad_colour_is_refused(self, tenancy):
        with pytest.raises(services.InboxError):
            _label(tenancy.workspace, "Beacon", color="not-a-colour")

    def test_no_colour_gets_the_default(self, tenancy):
        from apps.inbox.models import DEFAULT_LABEL_COLOR

        assert _label(tenancy.workspace, "Plain", color="").color == DEFAULT_LABEL_COLOR


class TestApplyingOne:
    def test_it_is_idempotent(self, tenancy, conversation):
        label = _label(tenancy.workspace)

        assert services.apply_label(conversation, label) is True
        assert services.apply_label(conversation, label) is False
        assert _names(conversation) == ["Refunds"]

    def test_a_label_from_another_workspace_is_refused(self, tenancy, other_tenancy, conversation):
        """``link.workspace``, ``link.conversation.workspace`` and
        ``link.label.workspace`` are three answers to one question, and a form
        field naming another tenant's label is an IDOR no URL fuzzer reaches."""
        from apps.inbox.models import WorkspaceMismatchError

        theirs = _label(other_tenancy.workspace, "Theirs")

        with pytest.raises(WorkspaceMismatchError):
            services.apply_label(conversation, theirs)

    def test_a_thread_is_capped(self, tenancy, conversation):
        for index in range(MAX_LABELS_PER_CONVERSATION):
            services.apply_label(conversation, _label(tenancy.workspace, f"L{index}"))

        with pytest.raises(services.InboxError):
            services.apply_label(conversation, _label(tenancy.workspace, "One too many"))

    def test_removing_reports_whether_anything_moved(self, tenancy, conversation):
        label = _label(tenancy.workspace)
        services.apply_label(conversation, label)

        assert services.remove_label(conversation, label) is True
        assert services.remove_label(conversation, label) is False

    def test_deleting_a_label_takes_it_off_every_thread(self, tenancy, conversation):
        label = _label(tenancy.workspace)
        services.apply_label(conversation, label)

        label.delete()

        assert _names(conversation) == []


class TestTheEndpoints:
    def test_an_agent_can_label_and_unlabel(self, tenancy, agent_client, url_for, conversation):
        label = _label(tenancy.workspace)

        added = agent_client.post(url_for("add_label", conversation_id=conversation.pk), {"label": str(label.pk)})
        assert added.status_code == 204
        assert _names(conversation) == ["Refunds"]

        removed = agent_client.post(url_for("remove_label", conversation_id=conversation.pk, label_id=label.pk))
        assert removed.status_code == 204
        assert _names(conversation) == []

    def test_a_viewer_cannot(self, tenancy, viewer_client, url_for, conversation):
        label = _label(tenancy.workspace)

        response = viewer_client.post(url_for("add_label", conversation_id=conversation.pk), {"label": str(label.pk)})

        assert response.status_code == 403
        assert _names(conversation) == []

    def test_another_workspaces_label_is_a_404(self, tenancy, other_tenancy, agent_client, url_for, conversation):
        theirs = _label(other_tenancy.workspace, "Theirs")

        response = agent_client.post(url_for("add_label", conversation_id=conversation.pk), {"label": str(theirs.pk)})

        assert response.status_code == 404

    def test_bulk_labelling_skips_what_it_cannot_reach(
        self, tenancy, other_tenancy, agent_client, url_for, conversation
    ):
        """A bulk action is issued against a list rendered seconds ago, so an id
        that has since become unreachable is ordinary rather than an attack."""
        from apps.messaging.services import open_conversation

        theirs = open_conversation(
            workspace=other_tenancy.workspace,
            contact=other_tenancy.workspace.contacts.create(first_name="Rival"),
            connection=other_tenancy.workspace.channelconnections.create(
                platform="telegram", display_name="theirs", external_id="bot-rival"
            ),
        )
        label = _label(tenancy.workspace)

        response = agent_client.post(
            url_for("bulk_label"),
            {"label": str(label.pk), "conversation": [str(conversation.pk), str(theirs.pk)]},
        )

        assert response.status_code == 204
        assert _names(conversation) == ["Refunds"]
        assert not ConversationLabelLink.objects.for_workspace(other_tenancy.workspace).exists()


class TestTheFilter:
    def test_it_narrows_the_list(self, tenancy, conversation, connection):
        from apps.contacts.models import Contact
        from apps.messaging.services import open_conversation

        other = open_conversation(
            workspace=tenancy.workspace,
            contact=Contact.objects.create(workspace=tenancy.workspace, first_name="Bob"),
            connection=connection,
        )
        label = _label(tenancy.workspace)
        services.apply_label(conversation, label)

        rows = selectors.conversations_for(tenancy.workspace, viewer=tenancy.owner, label=str(label.pk))

        assert list(rows) == [conversation]
        assert other not in list(rows)

    def test_an_unparseable_label_filters_nothing_rather_than_500ing(self, tenancy, conversation):
        """This endpoint is polled every three seconds; a stale bookmark must
        not be a 500 anybody can reach."""
        rows = selectors.conversations_for(tenancy.workspace, viewer=tenancy.owner, label="not-a-uuid")

        assert list(rows) == []


class TestTheSettingsPage:
    def test_an_agent_can_manage_the_palette(self, tenancy, agent_client, url_for):
        assert agent_client.get(url_for("label_settings")).status_code == 200

        created = agent_client.post(url_for("label_create"), {"name": "Refunds", "color": "#3B82F6"})
        assert created.status_code == 204
        label = ConversationLabel.objects.for_workspace(tenancy.workspace).get()

        renamed = agent_client.post(url_for("label_update", label_id=label.pk), {"name": "Billing", "color": "#EF4444"})
        assert renamed.status_code == 204
        label.refresh_from_db()
        assert (label.name, label.color) == ("Billing", "#EF4444")

        deleted = agent_client.post(url_for("label_delete", label_id=label.pk))
        assert deleted.status_code == 204
        assert not ConversationLabel.objects.for_workspace(tenancy.workspace).exists()

    def test_a_viewer_cannot_open_it(self, viewer_client, url_for):
        assert viewer_client.get(url_for("label_settings")).status_code == 403

    def test_a_duplicate_name_is_a_toast_not_a_500(self, tenancy, agent_client, url_for):
        """204 even though it refused: htmx drops HX-Trigger on a non-2xx, so a
        400 would swallow the very toast the operator needs to read. The same
        convention every mutation in this app follows."""
        _label(tenancy.workspace, "Refunds")

        response = agent_client.post(url_for("label_create"), {"name": "refunds"})

        assert response.status_code == 204
        assert "showToast" in response.headers["HX-Trigger"]
        assert ConversationLabel.objects.for_workspace(tenancy.workspace).count() == 1

    def test_the_page_counts_usage(self, tenancy, agent_client, url_for, conversation):
        """ "Delete" is destructive and the count is the only thing that says how
        destructive."""
        label = _label(tenancy.workspace)
        services.apply_label(conversation, label)

        page = agent_client.get(url_for("label_settings")).content.decode()

        assert "Refunds" in page
        assert selectors.label_usage(tenancy.workspace) == {label.pk: 1}


class TestTheRuleLabelCap:
    def test_a_full_thread_still_gets_a_label_it_is_missing(self, tenancy, conversation, connection, identity):
        """The cap is applied to what is *missing*, not to the rule's whole list.

        Slicing the rule's labels against the remaining room spends the last free
        slot on whichever sorts first — which may be one the thread already
        carries, silently dropping the one that was new.
        """
        from apps.flows.tests.routing_support import routing_adapter
        from apps.flows.tests.support import inbound as raw_event
        from apps.flows.triggers.pipeline import route_events
        from apps.inbox.models import InboxRule

        already = _label(tenancy.workspace, "Aaa already here")
        missing = _label(tenancy.workspace, "Zzz brand new")
        services.apply_label(conversation, already)
        # Fill the thread to exactly one slot short of the cap.
        for index in range(MAX_LABELS_PER_CONVERSATION - 2):
            services.apply_label(conversation, _label(tenancy.workspace, f"Filler {index}"))

        rule = InboxRule(
            workspace=tenancy.workspace,
            name="Both",
            condition_json={"channel": {"platforms": ["telegram"]}},
            actions_json=[
                {"type": "add_label", "label_id": str(already.pk)},
                {"type": "add_label", "label_id": str(missing.pk)},
            ],
        )
        rule.save()

        with routing_adapter(connection.platform):
            route_events(connection, [raw_event(connection, text="hi", user="u1")])

        assert "Zzz brand new" in _names(conversation)
