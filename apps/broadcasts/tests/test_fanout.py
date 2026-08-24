"""Fanout — SPEC §13.2, and the acceptance criteria that hang off it.

Three properties are load-bearing and each has a test that would fail loudly if
it regressed:

* **batches of 500**, resumed by cursor, so no single transaction holds a
  ten-thousand-contact audience open past ``ZOMBIE_AFTER``;
* **``run_at`` spread at the connection's own rate**, so a broadcast cannot fill
  every worker batch and starve a flow resume scheduled a second later;
* **zero duplicates on a forced re-run**, which SPEC §21 asks for directly.
"""

import pytest
from django.utils import timezone

from apps.broadcasts import handlers, services
from apps.broadcasts.models import BroadcastStatus, RecipientStatus
from apps.messaging import buckets
from apps.messaging.codes import Denial
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


def _fanout_actions(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_FANOUT)


def _send_actions(workspace):
    return ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_SEND)


def _run_fanout(action) -> None:
    """Run one fanout action and mark it done, the way the worker would.

    The handler on its own leaves the row ``pending``, so a test looking for the
    successor would find two — which is a fixture artefact, not a product one.
    """
    handlers.handle_broadcast_fanout(action.payload, action)
    ScheduledAction.objects.for_workspace(action.workspace_id).filter(pk=action.pk).update(status=ActionStatus.DONE)


def _run_due(workspace, *, limit: int = 10_000) -> int:
    """Run every due action for this workspace, without moving the clock.

    Not ``worker.drain()``: that claims across every tenant and would run the
    housekeeping chain too. This is the same claim-and-handle shape narrowed to
    the rows a test put there.
    """
    from apps.queueing.worker import process_action

    due = list(
        ScheduledAction.objects.for_workspace(workspace)
        .filter(status=ActionStatus.PENDING, run_at__lte=timezone.now())
        .order_by("run_at")[:limit]
    )
    for action in due:
        ScheduledAction.objects.for_workspace(workspace).filter(pk=action.pk).update(
            status=ActionStatus.RUNNING, attempts=action.attempts + 1
        )
        action.refresh_from_db()
        process_action(action)
    return len(due)


@pytest.mark.django_db
class TestScheduling:
    def test_scheduling_arms_one_fanout_action(self, tenancy, make_contacts, make_broadcast, connection):
        make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)

        services.schedule_broadcast(broadcast)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SCHEDULED
        assert _fanout_actions(tenancy.workspace).count() == 1

    def test_the_content_version_is_pinned_at_schedule_time(self, tenancy, make_contacts, make_broadcast, connection):
        """An edit while the queue drains must not change what the rest receive.

        The pin is what makes that true: the send passes ``flow_version``
        explicitly rather than letting the engine resolve "the published one".
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection, text="First wording")
        services.schedule_broadcast(broadcast)
        broadcast.refresh_from_db()
        pinned = broadcast.flow_version_id

        from apps.flows.services import save_draft

        save_draft(broadcast.flow, services.content_graph({"blocks": [{"type": "text", "text": "Rewritten"}]}))

        broadcast.refresh_from_db()
        assert broadcast.flow_version_id == pinned
        assert "First wording" in str(broadcast.flow_version.graph_json)

    def test_a_draft_with_no_message_is_refused(self, tenancy, make_contacts, connection):
        from apps.broadcasts.tests.conftest import EVERYONE

        make_contacts(1, connection=connection)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Empty", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        with pytest.raises(services.BroadcastError, match="Add a message"):
            services.schedule_broadcast(broadcast)

    def test_an_audience_nobody_can_be_messaged_in_is_refused(self, make_contacts, make_broadcast, connection):
        make_contacts(2, connection=connection, opted_out=True)
        broadcast = make_broadcast(connection=connection)

        with pytest.raises(services.BroadcastError, match="can be messaged"):
            services.schedule_broadcast(broadcast)

    def test_an_empty_audience_is_refused(self, make_broadcast, connection):
        broadcast = make_broadcast(connection=connection)

        with pytest.raises(services.BroadcastError, match="Nobody matches"):
            services.schedule_broadcast(broadcast)


@pytest.mark.django_db
class TestChunking:
    def test_it_chunks_at_the_spec_size_and_resumes_by_cursor(
        self, tenancy, make_contacts, make_broadcast, connection, monkeypatch
    ):
        """SPEC §13.2: "in batches of 500". Shrunk here so the test is seconds.

        The number is patched rather than the audience grown to 501, because
        what is being asserted is the *shape* — a bounded chunk plus a successor
        carrying a cursor — and that shape is identical at 5 and at 500.
        """
        monkeypatch.setattr(handlers, "CHUNK_SIZE", 5)
        make_contacts(12, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        action = _fanout_actions(tenancy.workspace).get()
        _run_fanout(action)

        assert broadcast.recipients.count() == 5
        successor = _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).get()
        assert successor.payload["cursor"]

        _run_fanout(successor)
        assert broadcast.recipients.count() == 10

    def test_it_stops_enqueueing_a_successor_when_the_audience_runs_out(
        self, tenancy, make_contacts, make_broadcast, connection, monkeypatch
    ):
        monkeypatch.setattr(handlers, "CHUNK_SIZE", 5)
        make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()

        handlers.handle_broadcast_fanout(action.payload, action)

        assert _fanout_actions(tenancy.workspace).count() == 1

    def test_every_send_carries_the_idempotency_key_the_spec_fixes(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        contacts = make_contacts(3, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()
        handlers.handle_broadcast_fanout(action.payload, action)

        keys = set(_send_actions(tenancy.workspace).values_list("idempotency_key", flat=True))

        assert keys == {f"broadcast:{broadcast.pk}:contact:{contact.pk}" for contact in contacts}

    def test_a_forced_re_run_of_the_fanout_produces_no_duplicate_sends(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """SPEC §21: "zero duplicate sends across 1k forced worker retries".

        Zombie recovery can re-run a handler whose transaction committed, so this
        is not hypothetical. Two independent guards make it safe — the unique
        ``(broadcast, contact)`` on the recipient row and the queue's idempotency
        key — and this asserts the pair, by running the same action four times.
        """
        make_contacts(6, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()

        for _ in range(4):
            handlers.handle_broadcast_fanout(action.payload, action)

        assert broadcast.recipients.count() == 6
        assert _send_actions(tenancy.workspace).count() == 6

    def test_skipped_contacts_get_a_row_with_their_reason(self, tenancy, make_contacts, make_broadcast, connection):
        make_contacts(2, connection=connection)
        make_contacts(3, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()

        handlers.handle_broadcast_fanout(action.payload, action)

        skipped = broadcast.recipients.filter(status=RecipientStatus.SKIPPED)
        assert skipped.count() == 3
        assert set(skipped.values_list("reason", flat=True)) == {Denial.OPTED_OUT.value}
        # And only the eligible ones cost a queue row.
        assert _send_actions(tenancy.workspace).count() == 2


@pytest.mark.django_db
class TestRateSpread:
    def test_run_at_is_spread_at_the_connections_configured_rate(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """The mechanism that stops a 10k fanout starving transactional work.

        The claim query is ``run_at <= now() ORDER BY run_at``, so ten thousand
        rows due at once would fill every batch. Spreading at
        ``buckets.rate_for`` leaves about ``rate`` rows due per second and an
        ordinary action — always due "now" — sorts ahead of the rest.

        Asserted against ``rate_for`` rather than a literal, because a second
        copy of the rate here would be the second throttle the issue forbids.
        """
        make_contacts(4, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()
        handlers.handle_broadcast_fanout(action.payload, action)

        broadcast.refresh_from_db()
        rate = buckets.rate_for(connection.platform)
        offsets = sorted(
            (row.run_at - broadcast.started_at).total_seconds() for row in _send_actions(tenancy.workspace)
        )

        assert offsets == pytest.approx([index / rate for index in range(4)], abs=0.001)

    def test_the_spread_continues_across_chunks(self, tenancy, make_contacts, make_broadcast, connection, monkeypatch):
        """Restarting the spread every chunk would put 500 rows at the same instant.

        Which is the starvation this exists to prevent, arriving five hundred at
        a time instead of ten thousand at once.
        """
        monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
        make_contacts(6, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)

        action = _fanout_actions(tenancy.workspace).get()
        _run_fanout(action)
        successor = _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).get()
        _run_fanout(successor)

        run_ats = sorted(_send_actions(tenancy.workspace).values_list("run_at", flat=True))
        assert len(set(run_ats)) == 6

    def test_a_backlog_is_not_all_due_at_once(self, tenancy, make_contacts, make_broadcast, connection):
        """The property the spread buys, stated the way it actually holds.

        Starvation is a *temporal* problem, not an ordering one: the claim query
        takes the fifty oldest rows that are already due, so what protects an
        ordinary action is that most of the broadcast is not due yet. Asserting
        it through ``claim_batch`` would prove nothing — every broadcast row
        would be in the future and the claim would be empty either way — so this
        asserts the arithmetic the claim then rides on.
        """
        import math

        make_contacts(30, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()
        _run_fanout(action)

        rate = buckets.rate_for(connection.platform)
        run_ats = sorted(_send_actions(tenancy.workspace).values_list("run_at", flat=True))
        first = run_ats[0]

        # A second's worth of the backlog is due per second, and no more, so a
        # transactional action armed at any instant waits behind at most that
        # many rows rather than behind all thirty.
        due_in_first_second = sum(1 for run_at in run_ats if (run_at - first).total_seconds() < 1)
        assert due_in_first_second <= math.ceil(rate) + 1
        assert due_in_first_second < len(run_ats)
        assert (run_ats[-1] - first).total_seconds() == pytest.approx(29 / rate, abs=0.01)


@pytest.mark.django_db
class TestFanoutStatus:
    def test_fanout_moves_the_broadcast_to_sending(self, tenancy, make_contacts, make_broadcast, connection):
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()

        handlers.handle_broadcast_fanout(action.payload, action)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENDING

    def test_an_audience_that_is_entirely_skipped_finishes_immediately(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """There is no send to settle it, so fanout has to settle it itself."""
        make_contacts(3, connection=connection, opted_out=True)
        broadcast = make_broadcast(connection=connection)
        # Schedule refuses a wholly ineligible audience, so this one is made
        # ineligible after the fact — which is also the real race: an audience
        # can opt out between scheduling and fanout.
        make_contacts(1, connection=connection, prefix="ok")
        services.schedule_broadcast(broadcast)
        broadcast.recipients.all().delete()
        from apps.messaging.models import ContactChannelIdentity

        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).update(opted_out_at=timezone.now())

        action = _fanout_actions(tenancy.workspace).get()
        handlers.handle_broadcast_fanout(action.payload, action)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENT
        assert broadcast.finished_at is not None


@pytest.mark.django_db
class TestStats:
    def test_stats_are_written_once_per_chunk(self, tenancy, make_contacts, make_broadcast, connection):
        """SPEC §13.2's "updated in batches", read literally."""
        make_contacts(4, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        action = _fanout_actions(tenancy.workspace).get()

        handlers.handle_broadcast_fanout(action.payload, action)

        broadcast.refresh_from_db()
        assert broadcast.stats["queued"] == 4
        assert broadcast.stats["pending"] == 4


@pytest.mark.django_db
def test_a_cancelled_broadcast_stops_expanding_its_audience(
    tenancy, make_contacts, make_broadcast, connection, monkeypatch
):
    """Half one of "cancellation must be real": no further scheduling.

    The successor row is never written, so the audience past this cursor is
    never expanded at all — as opposed to expanded and then thrown away.
    """
    monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
    make_contacts(9, connection=connection)
    broadcast = make_broadcast(connection=connection)
    services.schedule_broadcast(broadcast)

    action = _fanout_actions(tenancy.workspace).get()
    _run_fanout(action)
    assert broadcast.recipients.count() == 3

    services.cancel_broadcast(broadcast)
    successor = _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).first()
    assert successor is None, "the pending successor should have been cancelled with the broadcast"

    # And a successor that had already been claimed refuses itself.
    stale = _fanout_actions(tenancy.workspace).filter(status=ActionStatus.CANCELLED).first()
    if stale is not None:
        handlers.handle_broadcast_fanout(stale.payload, stale)
    assert broadcast.recipients.count() == 3


@pytest.mark.django_db
class TestSettleWaitsForFanout:
    """A broadcast is not finished just because nothing is pending.

    Fanout expands five hundred contacts at a time. Between two chunks the rows
    written so far can all reach a terminal state — and settling at that moment
    marks the broadcast ``sent``, after which the next chunk reads that status
    and returns without expanding anybody else. The rest of the audience is
    never messaged, the counters read complete, and ``broadcast.finished`` has
    already gone out.
    """

    def test_a_drained_first_chunk_does_not_finish_the_broadcast(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for, monkeypatch
    ):
        """The reachable shape: a chunk that failed and is waiting on its backoff.

        Thirty seconds is the first rung of SPEC §15's ladder and is easily long
        enough for a chunk's worth of sends to drain, so this is not a race that
        needs an unlucky millisecond — it needs one transient database error.
        """
        monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
        make_contacts(9, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform) as adapter:
            services.schedule_broadcast(broadcast)
            first = _fanout_actions(tenancy.workspace).get()
            _run_fanout(first)

            # The successor exists but has not run — the state a backoff leaves.
            assert _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).exists()

            # Every send written so far completes.
            for action in _send_actions(tenancy.workspace):
                handlers.handle_broadcast_send(action.payload, action)
            assert len(adapter.sends) == 3

            broadcast.refresh_from_db()
            assert broadcast.status == BroadcastStatus.SENDING, (
                "settling here would strand the six contacts fanout has not reached yet"
            )

            # And the successor still expands them.
            successor = _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).get()
            _run_fanout(successor)
            assert broadcast.recipients.count() == 6

    def test_the_housekeeping_sweep_will_not_finish_one_either(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for, monkeypatch
    ):
        """The sweep reaches the same state from the other side.

        It reconciles stranded recipients and then settles, so without the guard
        it would truncate an audience on its own schedule rather than waiting for
        an unlucky race.
        """
        from apps.broadcasts.housekeeping import settle_broadcasts

        monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
        make_contacts(9, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            services.schedule_broadcast(broadcast)
            _run_fanout(_fanout_actions(tenancy.workspace).get())
            for action in _send_actions(tenancy.workspace):
                handlers.handle_broadcast_send(action.payload, action)

            settle_broadcasts()

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENDING

    def test_the_last_chunk_can_still_settle_an_audience_it_wholly_skipped(
        self, tenancy, make_contacts, make_broadcast, connection
    ):
        """The guard must not deadlock the handler against its own row.

        A fanout action is ``running`` for the length of its own transaction, so
        an unqualified "is any fanout outstanding?" would stop the very chunk
        that has just exhausted the audience from finishing it.
        """
        make_contacts(3, connection=connection, opted_out=True, prefix="gone")
        make_contacts(1, connection=connection, prefix="ok")
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        broadcast.recipients.all().delete()

        from apps.messaging.models import ContactChannelIdentity

        ContactChannelIdentity.objects.for_workspace(tenancy.workspace).update(opted_out_at=timezone.now())
        action = _fanout_actions(tenancy.workspace).get()
        # Claimed, exactly as the worker leaves it while the handler runs.
        _fanout_actions(tenancy.workspace).filter(pk=action.pk).update(status=ActionStatus.RUNNING)
        action.refresh_from_db()

        handlers.handle_broadcast_fanout(action.payload, action)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SENT


@pytest.mark.django_db
def test_a_fanout_that_failed_for_good_leaves_the_broadcast_visibly_unfinished(
    tenancy, make_contacts, make_broadcast, connection, adapter_for, monkeypatch
):
    """A permanently failed chunk means the audience was never fully expanded.

    Not hypothetical: deleting a tag the filter names makes every remaining
    chunk raise until the retry budget is spent. Marking the broadcast ``sent``
    then would announce success for a send that reached a fraction of its
    audience. Leaving it at ``sending`` beside a red queue row is the state an
    operator can see — and cancel is the exit.
    """
    monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
    make_contacts(9, connection=connection)
    broadcast = make_broadcast(connection=connection)

    with adapter_for(connection.platform):
        services.schedule_broadcast(broadcast)
        _run_fanout(_fanout_actions(tenancy.workspace).get())

        # The successor exhausts its retries.
        _fanout_actions(tenancy.workspace).filter(status=ActionStatus.PENDING).update(
            status=ActionStatus.FAILED, last_error="ConditionValidationError: unknown tag"
        )
        for action in _send_actions(tenancy.workspace):
            handlers.handle_broadcast_send(action.payload, action)

    broadcast.refresh_from_db()
    assert broadcast.status == BroadcastStatus.SENDING

    # And the operator can still stop it.
    services.cancel_broadcast(broadcast)
    broadcast.refresh_from_db()
    assert broadcast.status == BroadcastStatus.CANCELLED
