"""The pages, end to end — every step renders and every mutation lands.

Thin on purpose. The behaviour lives in ``services`` and is tested there; what is
worth asserting at this level is that the wizard is reachable, that each step
renders without a template error, and that the four saves move a draft forward —
the things a refactor of the view layer would break silently.

Cross-tenant access is not tested here: ``tests/idor.py`` sweeps every one of
these routes with another tenant's ids and asserts 404, and duplicating that per
view would be a second, weaker copy of a suite that cannot be escaped.
"""

import pytest
from django.urls import reverse

from apps.broadcasts import services
from apps.broadcasts.models import Broadcast, BroadcastStatus
from apps.broadcasts.tests.conftest import EVERYONE


def _url(name, tenancy, **kwargs):
    return reverse(name, kwargs={"workspace_id": tenancy.workspace.pk, **kwargs})


@pytest.mark.django_db
class TestList:
    def test_the_list_renders_with_the_broadcastable_channels(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        make_contacts(1, connection=connection)
        make_broadcast(connection=connection, name="Spring sale")

        body = client_for(tenancy.owner).get(_url("broadcasts:list", tenancy)).content.decode()

        assert "Spring sale" in body
        assert connection.display_name in body

    def test_the_rows_partial_filters_by_status(self, tenancy, client_for, connection, make_broadcast):
        make_broadcast(connection=connection, name="A draft")

        rows = client_for(tenancy.owner).get(_url("broadcasts:rows", tenancy) + "?status=sent")

        assert b"A draft" not in rows.content

    def test_an_unknown_status_falls_back_rather_than_filtering_nothing(
        self, tenancy, client_for, connection, make_broadcast
    ):
        """The trap apps/flows/views.py documents: an if/elif lets `?status=bogus`
        skip the branch and quietly show rows the filter was meant to exclude."""
        make_broadcast(connection=connection, name="A draft")

        rows = client_for(tenancy.owner).get(_url("broadcasts:rows", tenancy) + "?status=bogus")

        assert b"A draft" in rows.content

    def test_creating_one_redirects_into_the_composer(self, tenancy, client_for, connection):
        response = client_for(tenancy.owner).post(
            _url("broadcasts:create", tenancy), {"name": "New one", "connection_id": str(connection.pk)}
        )

        broadcast = Broadcast.objects.for_workspace(tenancy.workspace).get()
        assert response.status_code == 204
        assert response.headers["HX-Redirect"] == _url("broadcasts:compose", tenancy, broadcast_id=broadcast.pk)


@pytest.mark.django_db
class TestWizard:
    @pytest.mark.parametrize("step", ["channel", "audience", "content", "schedule"])
    def test_every_step_renders(self, tenancy, client_for, connection, make_broadcast, step):
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).get(
            _url("broadcasts:wizard", tenancy, broadcast_id=broadcast.pk) + f"?step={step}"
        )

        assert response.status_code == 200

    def test_the_composer_page_renders_the_step_the_draft_has_reached(self, tenancy, client_for, connection):
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        body = (
            client_for(tenancy.owner)
            .get(_url("broadcasts:compose", tenancy, broadcast_id=broadcast.pk))
            .content.decode()
        )

        # No audience yet, so the audience step is where it opens.
        assert "Who receives this?" in body

    def test_a_scheduled_broadcast_opens_its_detail_page_instead(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        """A real 302, because this is a page and browsers reach it directly.

        ``HX-Redirect`` is a header only htmx acts on. This route is bookmarked,
        reloaded and reached with the Back button, and a 204 carrying that header
        renders as a blank page for every one of those.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        response = client_for(tenancy.owner).get(_url("broadcasts:compose", tenancy, broadcast_id=broadcast.pk))

        assert response.status_code == 302
        assert response.headers["Location"] == _url("broadcasts:detail", tenancy, broadcast_id=broadcast.pk)

    def test_saving_the_channel_moves_to_the_audience_step(self, tenancy, client_for, connection):
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_channel", tenancy, broadcast_id=broadcast.pk),
            {"connection_id": str(connection.pk)},
        )

        assert b"Who receives this?" in response.content

    def test_saving_the_audience_stores_the_document_and_moves_on(self, tenancy, client_for, connection, make_contacts):
        make_contacts(2, connection=connection)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_audience", tenancy, broadcast_id=broadcast.pk),
            {"filter": '{"match":"all","rules":[]}'},
        )

        broadcast.refresh_from_db()
        assert broadcast.target_filter_json == EVERYONE
        assert b"Message" in response.content

    def test_a_segment_is_copied_rather_than_linked(self, tenancy, client_for, connection, make_contacts):
        """A segment edited after a broadcast was scheduled must not change who
        it goes to, and one deleted must not take the audience with it."""
        from apps.contacts.models import Segment

        make_contacts(1, connection=connection)
        segment = Segment.objects.create(workspace=tenancy.workspace, name="Everyone", filter_json=EVERYONE)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        client_for(tenancy.owner).post(
            _url("broadcasts:save_audience", tenancy, broadcast_id=broadcast.pk),
            {"segment": str(segment.pk)},
        )

        broadcast.refresh_from_db()
        assert broadcast.target_filter_json == EVERYONE
        assert broadcast.segment_id == segment.pk

        segment.filter_json = {"match": "any", "rules": []}
        segment.save(update_fields=["filter_json"])
        broadcast.refresh_from_db()
        assert broadcast.target_filter_json == EVERYONE

    def test_scheduling_now_puts_it_in_the_queue_and_opens_the_detail_page(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        from apps.queueing.models import ActionType, ScheduledAction

        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_schedule", tenancy, broadcast_id=broadcast.pk), {"when": "now"}
        )

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SCHEDULED
        assert response.headers["HX-Redirect"] == _url("broadcasts:detail", tenancy, broadcast_id=broadcast.pk)
        assert (
            ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).exists()
        )

    def test_scheduling_later_reads_the_time_in_the_workspaces_timezone(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        """SPEC §13.1: "schedule now/later, flatpickr, workspace timezone".

        An operator picking 09:00 means nine in the morning where they work, and
        a server in UTC would otherwise send it at whatever nine UTC is for them.
        """
        from zoneinfo import ZoneInfo

        tenancy.workspace.timezone = "Australia/Sydney"
        tenancy.workspace.save(update_fields=["timezone"])
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        client_for(tenancy.owner).post(
            _url("broadcasts:save_schedule", tenancy, broadcast_id=broadcast.pk),
            {"when": "later", "scheduled_at": "2099-06-01 09:00"},
        )

        broadcast.refresh_from_db()
        local = broadcast.scheduled_at.astimezone(ZoneInfo("Australia/Sydney"))
        assert (local.hour, local.minute) == (9, 0)

    def test_a_time_in_the_past_is_refused_on_the_form(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_schedule", tenancy, broadcast_id=broadcast.pk),
            {"when": "later", "scheduled_at": "2001-01-01 09:00"},
        )

        assert b"already passed" in response.content
        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.DRAFT


@pytest.mark.django_db
class TestDuplicateAndDelete:
    def test_a_duplicate_comes_back_as_a_draft_with_the_same_message(
        self, tenancy, client_for, connection, make_contacts, make_broadcast
    ):
        """Sending is an act; a copy that arrived already scheduled would send itself."""
        make_contacts(1, connection=connection)
        original = make_broadcast(connection=connection, text="Original wording")
        services.schedule_broadcast(original)

        client_for(tenancy.owner).post(_url("broadcasts:duplicate", tenancy, broadcast_id=original.pk))

        copy = Broadcast.objects.for_workspace(tenancy.workspace).exclude(pk=original.pk).get()
        assert copy.status == BroadcastStatus.DRAFT
        assert copy.scheduled_at is None
        assert copy.stats == {}
        assert "Original wording" in str(copy.flow.versions.first().graph_json)
        assert copy.flow_id != original.flow_id

    def test_deleting_a_draft_takes_its_private_mini_flow_with_it(
        self, tenancy, client_for, connection, make_broadcast
    ):
        from apps.flows.models import Flow

        broadcast = make_broadcast(connection=connection)
        flow_id = broadcast.flow_id

        client_for(tenancy.owner).post(_url("broadcasts:delete", tenancy, broadcast_id=broadcast.pk))

        assert not Broadcast.objects.for_workspace(tenancy.workspace).exists()
        assert not Flow.objects.for_workspace(tenancy.workspace).filter(pk=flow_id).exists()

    def test_a_flow_moved_out_of_the_reserved_folder_survives(self, tenancy, client_for, connection, make_broadcast):
        """Moving it out of the Broadcasts folder is how an operator adopts it."""
        from apps.flows.models import Flow
        from apps.flows.services import set_folder

        broadcast = make_broadcast(connection=connection)
        flow_id = broadcast.flow_id
        set_folder(broadcast.flow, "Mine now")

        client_for(tenancy.owner).post(_url("broadcasts:delete", tenancy, broadcast_id=broadcast.pk))

        assert Flow.objects.for_workspace(tenancy.workspace).filter(pk=flow_id).exists()

    def test_the_mini_flow_is_filed_under_the_reserved_folder(self, connection, make_broadcast):
        broadcast = make_broadcast(connection=connection)

        assert broadcast.flow.folder == services.BROADCAST_FOLDER


@pytest.mark.django_db
class TestDeletionKeepsWhatRan:
    """Deleting a broadcast must not cascade away the runs it started.

    ``FlowExecution.flow`` is ``on_delete=CASCADE``, so removing the private
    mini-flow of a broadcast that sent would take one execution per recipient
    with it — including the ones still parked waiting for a button press. A
    cascade is not ``start_flow``'s supersede, either: it disarms nothing, so the
    queue rows that would have resumed those executions outlive them.
    """

    def test_a_sent_broadcasts_flow_and_executions_survive_deletion(
        self, tenancy, client_for, connection, make_contacts, make_broadcast, adapter_for
    ):
        from apps.broadcasts import handlers
        from apps.flows.models import Flow, FlowExecution
        from apps.queueing.models import ActionType, ScheduledAction

        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        with adapter_for(connection.platform):
            services.schedule_broadcast(broadcast)
            fanout = (
                ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
            )
            handlers.handle_broadcast_fanout(fanout.payload, fanout)
            ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(status="done")
            for action in ScheduledAction.objects.for_workspace(tenancy.workspace).filter(
                type=ActionType.BROADCAST_SEND
            ):
                handlers.handle_broadcast_send(action.payload, action)

        broadcast.refresh_from_db()
        flow_id = broadcast.flow_id
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 2

        client_for(tenancy.owner).post(_url("broadcasts:delete", tenancy, broadcast_id=broadcast.pk))

        assert not Broadcast.objects.for_workspace(tenancy.workspace).exists()
        assert Flow.objects.for_workspace(tenancy.workspace).filter(pk=flow_id).exists()
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 2

    def test_a_draft_that_never_ran_still_takes_its_flow_with_it(self, tenancy, client_for, connection, make_broadcast):
        """Nothing ran, so there is nothing to preserve and no clutter to leave."""
        from apps.flows.models import Flow

        broadcast = make_broadcast(connection=connection)
        flow_id = broadcast.flow_id

        client_for(tenancy.owner).post(_url("broadcasts:delete", tenancy, broadcast_id=broadcast.pk))

        assert not Flow.objects.for_workspace(tenancy.workspace).filter(pk=flow_id).exists()


@pytest.mark.django_db
class TestListTruncation:
    """A silent cap reads as "the older ones were deleted"."""

    def test_the_page_says_when_it_has_truncated(self, tenancy, client_for, connection, monkeypatch):
        from apps.broadcasts import views

        monkeypatch.setattr(views, "PAGE_SIZE", 2)
        for index in range(3):
            services.create_broadcast(
                workspace=tenancy.workspace, name=f"Send {index}", connection=connection, user=tenancy.owner
            )

        body = client_for(tenancy.owner).get(_url("broadcasts:rows", tenancy)).content.decode()

        assert "Showing the 2 most recent" in body

    def test_it_stays_quiet_when_everything_fits(self, tenancy, client_for, connection, make_broadcast):
        make_broadcast(connection=connection)

        body = client_for(tenancy.owner).get(_url("broadcasts:rows", tenancy)).content.decode()

        assert "most recent" not in body


@pytest.mark.django_db
class TestMediaBlocksAcceptEitherShape:
    """One field for "the picture", two schema keys underneath.

    apps/flows/engine/nodes/send_message.py::_media_url treats a non-empty
    media_id as a library id and *raises* when it does not resolve — which drops
    the whole message rather than the picture. So a pasted link has to be stored
    under `url`, and the composer decides which by the same UUID rule the node
    uses.
    """

    def _saved_config(self, tenancy, client_for, connection, blocks):
        from apps.broadcasts.tests.conftest import EVERYONE

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Media", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        import json

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_content", tenancy, broadcast_id=broadcast.pk),
            {"mode": "message", "config": json.dumps({"blocks": blocks})},
        )
        assert response.status_code == 200
        broadcast.refresh_from_db()
        return services.node_config(broadcast.flow.versions.first().graph_json)

    def test_a_url_is_stored_under_url(self, tenancy, client_for, connection):
        config = self._saved_config(
            tenancy, client_for, connection, [{"type": "image", "url": "https://example.test/a.png"}]
        )

        assert config["blocks"][0] == {"type": "image", "url": "https://example.test/a.png"}

    def test_a_library_id_is_stored_under_media_id(self, tenancy, client_for, connection):
        from apps.media_library.models import MediaAsset

        asset = MediaAsset.objects.create(
            workspace=tenancy.workspace, filename="a.png", kind="image", mime="image/png", size=1, file="media/a.png"
        )
        config = self._saved_config(tenancy, client_for, connection, [{"type": "image", "media_id": str(asset.pk)}])

        assert config["blocks"][0] == {"type": "image", "media_id": str(asset.pk)}

    def test_the_composer_script_routes_by_the_same_uuid_rule_as_the_send_node(self):
        """Structural: the two must agree about what a library id looks like, or
        a value routed one way here is read the other way there."""
        from pathlib import Path

        script = (Path(__file__).resolve().parents[3] / "templates" / "broadcasts" / "_compose_script.html").read_text()

        assert "isLibraryId" in script
        assert "media.url = block.media" in script
        assert "media.media_id = block.media" in script
