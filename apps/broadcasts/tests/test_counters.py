"""Live counters and the polling transport (SPEC §13.2, §14's 304s).

Two separable claims:

* the **numbers** are computed from the rows that hold the truth, so they
  reconcile by construction and pick up a delivery receipt with no receipt path
  of this app's own;
* the **transport** is a conditional GET, so an idle broadcast page costs a
  request line rather than a rendered fragment every three seconds.
"""

import pytest
from django.urls import reverse

from apps.broadcasts import handlers, services
from apps.messaging.models import Message, MessageStatus
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction


def _run(workspace, broadcast):
    services.schedule_broadcast(broadcast)
    fanout = ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
    handlers.handle_broadcast_fanout(fanout.payload, fanout)
    # Marked done the way the worker would. A fanout row left ``pending`` is work
    # still owed, and ``services.fanout_outstanding`` reads it as such — which is
    # the whole point of that guard, so a helper that skipped this would be
    # testing against a state the product never reaches.
    ScheduledAction.objects.for_workspace(workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)
    for action in ScheduledAction.objects.for_workspace(workspace).filter(type=ActionType.BROADCAST_SEND):
        handlers.handle_broadcast_send(action.payload, action)
    broadcast.refresh_from_db()


def _url(name, tenancy, broadcast):
    return reverse(name, kwargs={"workspace_id": tenancy.workspace.pk, "broadcast_id": broadcast.pk})


@pytest.mark.django_db
class TestCounters:
    def test_they_reconcile(self, tenancy, make_contacts, make_broadcast, connection, adapter_for):
        make_contacts(4, connection=connection)
        make_contacts(2, connection=connection, opted_out=True, prefix="out")
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        counts = services.counters(broadcast)
        assert counts.queued == 6
        assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped
        assert counts.percent == 100
        assert counts.is_finished

    def test_delivered_follows_the_message_row(self, tenancy, make_contacts, make_broadcast, connection, adapter_for):
        """A delivery receipt updates ``Message.status`` in ``apps.messaging``.

        This app joins that column rather than keeping its own counter, which is
        why it needs no receipt path and why the number cannot drift from the
        thread an agent is looking at.
        """
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        assert services.counters(broadcast).delivered == 0
        Message.objects.for_workspace(tenancy.workspace).update(status=MessageStatus.DELIVERED)
        assert services.counters(broadcast).delivered == 2

        Message.objects.for_workspace(tenancy.workspace).update(status=MessageStatus.READ)
        counts = services.counters(broadcast)
        assert counts.delivered == 2
        assert counts.read == 2

    def test_a_send_that_ultimately_failed_is_counted_as_failed(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """The recipient was marked sent when the facade accepted it, and a
        ``send_retry`` can exhaust its budget hours later. Reading the joined
        status back is what keeps the totals honest."""
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        message = Message.objects.for_workspace(tenancy.workspace).first()
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(status=MessageStatus.FAILED)

        counts = services.counters(broadcast)
        assert counts.sent == 1
        assert counts.failed == 1
        assert counts.queued == counts.sent + counts.failed + counts.cancelled + counts.skipped

    def test_stats_are_written_back_only_when_they_change(
        self, tenancy, make_contacts, make_broadcast, connection, adapter_for
    ):
        """SPEC §13.2's "updated in batches": a poll must not put an UPDATE on a GET."""
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)

        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        broadcast.refresh_from_db()
        before = broadcast.updated_at
        services.release_stats(broadcast)
        broadcast.refresh_from_db()

        assert broadcast.updated_at == before


@pytest.mark.django_db
class TestPolling:
    def test_an_unchanged_poll_answers_304(
        self, tenancy, client_for, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)
        client = client_for(tenancy.owner)
        url = _url("broadcasts:counters", tenancy, broadcast)

        first = client.get(url)
        assert first.status_code == 200
        etag = first.headers["ETag"]

        second = client.get(url, headers={"if-none-match": etag})

        assert second.status_code == 304
        assert second.content == b""

    def test_a_changed_counter_answers_200_with_a_new_tag(
        self, tenancy, client_for, make_contacts, make_broadcast, connection, adapter_for
    ):
        """The token is built from the figures the fragment renders, so it cannot
        disagree with the markup — the property apps/common/polling.py asks a
        caller to arrange."""
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)
        client = client_for(tenancy.owner)
        url = _url("broadcasts:counters", tenancy, broadcast)
        etag = client.get(url).headers["ETag"]

        Message.objects.for_workspace(tenancy.workspace).update(status=MessageStatus.DELIVERED)
        again = client.get(url, headers={"if-none-match": etag})

        assert again.status_code == 200
        assert again.headers["ETag"] != etag

    def test_the_response_is_never_stored_by_a_browser_cache(
        self, tenancy, client_for, make_contacts, make_broadcast, connection
    ):
        """The revalidation is JavaScript remembering the tag, not the HTTP cache.

        Letting the cache in as well would replay a 304 to the caller as a 200
        from cache, losing the "did anything change?" answer on the way.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        client = client_for(tenancy.owner)

        response = client.get(_url("broadcasts:counters", tenancy, broadcast))

        assert response.headers["Cache-Control"] == "no-store"

    def test_the_status_chip_lives_in_the_polled_fragment(
        self, tenancy, client_for, make_contacts, make_broadcast, connection
    ):
        """It changes while somebody is looking at the page.

        A copy in the page header would sit at "Scheduled" while the counters
        beside it filled in — which is what it did until this test existed.
        """
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        client = client_for(tenancy.owner)

        fragment = client.get(_url("broadcasts:counters", tenancy, broadcast)).content.decode()

        assert "status-pill-draft" in fragment

    def test_the_detail_page_carries_the_htmx_304_fix(
        self, tenancy, client_for, make_contacts, make_broadcast, connection
    ):
        """htmx 2's stock 2xx rule matches "304" and would swap an empty body
        over the counters. The page must prepend a rule that says otherwise, or
        every unchanged poll blanks the pane."""
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)
        client = client_for(tenancy.owner)

        body = client.get(_url("broadcasts:detail", tenancy, broadcast)).content.decode()

        assert "responseHandling.unshift" in body
        assert "'304'" in body
        assert "If-None-Match" in body


@pytest.mark.django_db
class TestRecipientList:
    def test_it_shows_who_was_skipped_and_why(
        self, tenancy, client_for, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(1, connection=connection)
        make_contacts(1, connection=connection, opted_out=True, prefix="Refused")
        broadcast = make_broadcast(connection=connection)
        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        body = client_for(tenancy.owner).get(_url("broadcasts:recipients", tenancy, broadcast)).content.decode()

        assert "Refused0" in body
        assert "opted out" in body

    def test_an_unknown_status_filter_falls_back_rather_than_erroring(
        self, tenancy, client_for, make_contacts, make_broadcast, connection
    ):
        make_contacts(1, connection=connection)
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).get(
            _url("broadcasts:recipients", tenancy, broadcast) + "?status=../../etc/passwd"
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestProgressWhileExpanding:
    """The bar carries no value until the denominator is real.

    ``queued`` counts the recipients fanout has written *so far*, so a
    percentage against it walks backwards every time a chunk of five hundred
    lands — and the moment an operator is most likely to be watching is the one
    where the indicator looks broken.
    """

    def test_the_bar_is_indeterminate_between_chunks(
        self, tenancy, client_for, make_contacts, make_broadcast, connection, monkeypatch
    ):
        from apps.broadcasts import handlers
        from apps.queueing.models import ActionStatus

        monkeypatch.setattr(handlers, "CHUNK_SIZE", 3)
        make_contacts(9, connection=connection)
        broadcast = make_broadcast(connection=connection)
        services.schedule_broadcast(broadcast)
        fanout = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.BROADCAST_FANOUT).get()
        handlers.handle_broadcast_fanout(fanout.payload, fanout)
        ScheduledAction.objects.for_workspace(tenancy.workspace).filter(pk=fanout.pk).update(status=ActionStatus.DONE)

        body = client_for(tenancy.owner).get(_url("broadcasts:counters", tenancy, broadcast)).content.decode()

        assert "bc-bar-indeterminate" in body
        assert "Working out who this reaches" in body
        assert "aria-valuenow" not in body

    def test_it_becomes_a_real_percentage_once_the_audience_is_resolved(
        self, tenancy, client_for, make_contacts, make_broadcast, connection, adapter_for
    ):
        make_contacts(2, connection=connection)
        broadcast = make_broadcast(connection=connection)
        with adapter_for(connection.platform):
            _run(tenancy.workspace, broadcast)

        body = client_for(tenancy.owner).get(_url("broadcasts:counters", tenancy, broadcast)).content.decode()

        assert "bc-bar-indeterminate" not in body
        assert 'aria-valuenow="100"' in body
