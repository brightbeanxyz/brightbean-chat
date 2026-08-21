"""Draft and publish semantics, including the concurrency the issue asks about.

The threaded tests use ``transaction=True`` because that is the only way to get
real, separate database connections: the default ``django_db`` wraps each test
in one transaction on one connection, which would hide exactly the interleaving
these tests exist to force.
"""

import threading
from typing import Any

import pytest
from django.db import connection

from apps.flows.fixtures import graph_for
from apps.flows.models import Flow, FlowStatus, FlowVersion
from apps.flows.schema import empty_graph
from apps.flows.services import (
    FlowValidationError,
    archive_flow,
    create_flow,
    duplicate_flow,
    latest_version,
    publish,
    published_version,
    restore_flow,
    save_draft,
)


def _versions(workspace: Any, flow: Flow) -> Any:
    return FlowVersion.objects.for_workspace(workspace).filter(flow=flow).order_by("version")


@pytest.mark.django_db
class TestDraftSaves:
    def test_editing_updates_the_latest_draft_in_place(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        graph = graph_for("send_message")

        first = save_draft(flow, graph, user=tenancy.owner)
        second = save_draft(flow, graph, user=tenancy.owner)

        assert first.pk == second.pk
        assert [v.version for v in _versions(tenancy.workspace, flow)] == [1]

    def test_editing_a_published_flow_opens_the_next_version(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        draft = save_draft(flow, graph_for("action"), user=tenancy.owner)

        assert draft.version == 2
        assert draft.published is False
        assert published_version(flow).version == 1

    def test_saving_does_not_reassign_created_by(self, tenancy):
        """`created_by` records who opened the revision. Rewriting it on every
        autosave made it name whoever last had the flow open instead."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)
        editor = tenancy.user_for("editor")

        save_draft(flow, graph_for("send_message"), user=editor)

        assert latest_version(flow).created_by_id == tenancy.owner.pk

    def test_the_saved_graph_is_what_comes_back(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        graph = graph_for("condition")

        save_draft(flow, graph, user=tenancy.owner)

        assert latest_version(flow).graph_json == graph


@pytest.mark.django_db
class TestPublish:
    def test_it_returns_the_findings_it_validated_against(self, tenancy):
        """The caller needs them for its response and publish has just computed
        them; returning only the version made every caller validate twice."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)

        result = publish(flow, user=tenancy.owner)

        assert result.version.published is True
        assert result.validation.is_publishable is True

    def test_publishing_flips_the_flag_and_activates_the_flow(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)

        version = publish(flow, user=tenancy.owner).version
        flow.refresh_from_db()

        assert version.published is True
        assert flow.status == FlowStatus.ACTIVE

    def test_publishing_a_second_version_unpublishes_the_first(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        first = publish(flow, user=tenancy.owner).version
        save_draft(flow, graph_for("action"), user=tenancy.owner)

        second = publish(flow, user=tenancy.owner).version
        first.refresh_from_db()

        assert (first.published, second.published) == (False, True)
        assert _versions(tenancy.workspace, flow).filter(published=True).count() == 1

    def test_errors_block_publishing_and_change_nothing(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        graph = graph_for("send_message")
        graph["edges"].append({"id": "dangle", "source": "subject", "sourceHandle": "default", "target": "nowhere"})
        save_draft(flow, graph, user=tenancy.owner)

        with pytest.raises(FlowValidationError) as caught:
            publish(flow, user=tenancy.owner)

        flow.refresh_from_db()
        assert flow.status == FlowStatus.DRAFT
        assert published_version(flow) is None
        assert [issue.code for issue in caught.value.result.errors] == ["dangling_edge"]

    def test_an_empty_flow_saves_but_will_not_publish(self, tenancy):
        """The builder autosaves an empty canvas the moment a flow is created.
        Refusing that save would lose work; refusing to publish it is right."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, empty_graph(), user=tenancy.owner)

        with pytest.raises(FlowValidationError) as caught:
            publish(flow, user=tenancy.owner)

        assert [issue.code for issue in caught.value.result.errors] == ["no_entry_node"]

    def test_warnings_do_not_block_publishing(self, tenancy):
        """SPEC §9.1: capability findings are non-blocking."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        graph = graph_for("send_message")
        # Unreachable node: a warning, and the only finding in this graph.
        graph["nodes"].append(
            {"id": "orphan", "type": "note", "position": {"x": 0, "y": 200}, "config": {"text": "TODO"}}
        )
        graph["nodes"].append(
            {
                "id": "stranded",
                "type": "action",
                "position": {"x": 0, "y": 400},
                "config": {"actions": [{"verb": "close_conversation"}]},
            }
        )
        graph["edges"].append({"id": "loop", "source": "sink", "sourceHandle": "default", "target": "stranded"})
        save_draft(flow, graph, user=tenancy.owner)

        assert publish(flow, user=tenancy.owner).version.published is True


@pytest.mark.django_db
class TestListActions:
    def test_duplicating_copies_the_graph_but_never_the_publication(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", folder="Onboarding")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        copy = duplicate_flow(flow, user=tenancy.owner)

        assert copy.name == "Welcome (copy)"
        assert copy.folder == "Onboarding"
        assert copy.status == FlowStatus.DRAFT
        assert published_version(copy) is None
        assert latest_version(copy).graph_json == latest_version(flow).graph_json

    def test_a_name_at_the_field_limit_still_gets_the_copy_suffix(self, tenancy):
        """Slicing the composed string dropped the suffix entirely, so the copy
        came out with a name identical to its original."""
        flow = create_flow(workspace=tenancy.workspace, name="x" * 200)

        copy = duplicate_flow(flow, user=tenancy.owner)

        assert copy.name.endswith(" (copy)")
        assert len(copy.name) == 200
        assert copy.name != flow.name

    def test_archiving_and_restoring_track_whether_anything_is_published(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        archive_flow(flow)
        assert flow.status == FlowStatus.ARCHIVED

        restore_flow(flow)
        assert flow.status == FlowStatus.ACTIVE

    def test_restoring_a_never_published_flow_returns_it_to_draft(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        archive_flow(flow)

        restore_flow(flow)

        assert flow.status == FlowStatus.DRAFT


def _in_threads(work: Any, count: int) -> list[BaseException]:
    """Run ``work`` in ``count`` threads at once; return whatever it raised."""
    barrier = threading.Barrier(count)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            barrier.wait(timeout=10)
            work()
        except BaseException as exc:  # noqa: BLE001 - the point is to report anything at all
            failures.append(exc)
        finally:
            # Each thread opened its own connection; leaving them open makes the
            # test database undroppable at teardown.
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return failures


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_concurrent_saves_never_duplicate_a_version_number(self, tenancy):
        """Without the row lock in save_draft, every thread reads the same
        "latest" and races to allocate the same next number — the unique
        constraint then turns one save into a 500 instead of a save."""
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)
        publish(flow, user=tenancy.owner)

        failures = _in_threads(lambda: save_draft(flow, graph_for("action"), user=tenancy.owner), count=6)

        assert not failures, failures
        numbers = [version.version for version in _versions(tenancy.workspace, flow)]
        assert numbers == [1, 2]

    def test_concurrent_publishes_leave_exactly_one_published_version(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(flow, graph_for("send_message"), user=tenancy.owner)

        failures = _in_threads(lambda: publish(flow, user=tenancy.owner), count=6)

        assert not failures, failures
        assert _versions(tenancy.workspace, flow).filter(published=True).count() == 1
        flow.refresh_from_db()
        assert flow.status == FlowStatus.ACTIVE

    def test_concurrent_saves_on_a_fresh_flow_stay_on_version_one(self, tenancy):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")

        failures = _in_threads(lambda: save_draft(flow, graph_for("send_message"), user=tenancy.owner), count=6)

        assert not failures, failures
        assert [version.version for version in _versions(tenancy.workspace, flow)] == [1]
