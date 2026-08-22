"""SPEC §9.2's loop cap, and the admin notification that comes with it.

    blocks_since_pause resets on Wait/Schedule. If it reaches 30 -> Fail("loop
    cap"), notify workspace admins (in-app notification), status failed.

SPEC §9.1 allows cycles on purpose — "cycles allowed (caps protect at runtime)"
— so this is the only thing standing between a flow an author can draw in three
clicks and a worker in an infinite loop. It is also one of the Layer-3 gate's
five conditions, which is why the count is asserted exactly rather than
approximately.
"""

import pytest

from apps.flows.engine import LOOP_CAP, start_flow
from apps.flows.engine.results import Continue, Wait
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import contact_for, edge, graph, node, node_runtime, published_flow
from apps.notifications.models import Notification

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def _ring(size: int = 3) -> dict:
    """A cycle with no pause anywhere in it — the shape the cap exists for.

    Three nodes rather than a self-edge, because a self-edge would leave the
    graph with no entry node and never reach the runner at all.
    """
    nodes = [node(f"n{index}", "action", NOOP_ACTION, x=index * 200) for index in range(size)]
    edges = [edge(f"n{index}", "default", f"n{(index + 1) % size}") for index in range(size)]
    # Break one incoming edge so n0 is the entry; the ring closes through the
    # last node, which is enough to loop forever.
    edges = [e for e in edges if e["target"] != "n0"]
    edges.append(edge(f"n{size - 1}", "default", "n1"))
    return graph(nodes, edges)


@pytest.mark.django_db
class TestLoopCap:
    def test_a_looping_flow_halts_at_thirty_blocks(self, tenancy):
        flow = published_flow(tenancy.workspace, _ring())
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert execution.blocks_since_pause == LOOP_CAP
        assert "loop cap" in execution.last_error

    def test_the_workspace_admins_are_notified(self, tenancy):
        flow = published_flow(tenancy.workspace, _ring(), name="Runaway")
        contact = contact_for(tenancy.workspace)

        start_flow(contact, flow, started_by=StartedBy.API)

        notifications = Notification.objects.filter(event_type="flow_loop_cap_hit")
        assert notifications.exists()
        # Copy comes from the registered event, not from an f-string at the call
        # site — so this asserts the context reached it, not the wording.
        assert "Runaway" in notifications.first().title
        recipients = {notification.user_id for notification in notifications}
        assert tenancy.owner.pk in recipients
        assert tenancy.user_for("viewer").pk not in recipients

    def test_a_database_error_in_notify_does_not_undo_the_failure(self, tenancy, monkeypatch):
        """The savepoint. Catching a database error inside ``atomic()`` is not enough.

        A failed statement aborts the Postgres transaction — every later query
        raises until it is rolled back — so swallowing one without a nested
        ``atomic()`` would take the ``failed`` status this notification exists to
        announce, and every node write before it, down with it.

        The failure has to be a *real* query for the test to mean anything: a
        mocked exception leaves the connection perfectly healthy, so this passes
        with or without the fix if you raise ``IntegrityError`` by hand.
        """
        from django.db import connection

        from apps.flows.engine import runner

        def _explode(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM a_table_that_does_not_exist")

        monkeypatch.setattr("apps.notifications.engine.notify", _explode)
        flow = published_flow(tenancy.workspace, _ring(), name="Runaway")
        contact = contact_for(tenancy.workspace)

        execution = runner.start_flow(contact, flow, started_by=StartedBy.API)

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.FAILED
        assert "loop cap" in execution.last_error

    def test_a_long_but_finite_flow_is_not_capped(self, tenancy):
        """29 blocks is fine; the cap is a loop detector, not a size limit."""
        size = LOOP_CAP - 1
        nodes = [node(f"n{index}", "action", NOOP_ACTION, x=index * 100) for index in range(size)]
        edges = [edge(f"n{index}", "default", f"n{index + 1}") for index in range(size - 1)]
        flow = published_flow(tenancy.workspace, graph(nodes, edges))
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.blocks_since_pause == size

    def test_a_pause_resets_the_budget(self, tenancy):
        """A loop *with* a wait in it is a conversation, not a runaway."""
        flow = published_flow(tenancy.workspace, _ring())
        contact = contact_for(tenancy.workspace)
        seen: list[str] = []

        def _execute(ctx):
            seen.append(ctx.node_id)
            if len(seen) == 5:
                return Wait({"type": "buttons", "token": "t1", "handles": {}})
            return Continue()

        with node_runtime("action", _execute):
            execution = start_flow(contact, flow, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.blocks_since_pause == 0

    def test_a_ring_of_start_flow_nodes_is_also_capped(self, tenancy):
        """SPEC §11.3's hand-off carries the counter, so mutual calls terminate.

        No node in this shape ever returns ``Continue``, so if the hand-off did
        not count as a block the counter would never move and the two flows
        would call each other until the stack ran out.
        """
        first = published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="First")
        second = published_flow(
            tenancy.workspace,
            graph([node("b", "start_flow", {"flow_id": str(first.pk)})]),
            name="Second",
        )
        from apps.flows.services import publish, save_draft

        save_draft(first, graph([node("a", "start_flow", {"flow_id": str(second.pk)})]))
        publish(first)
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, first, started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert "loop cap" in execution.last_error
