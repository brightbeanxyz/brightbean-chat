"""The two durable guards, including the races they exist to lose gracefully."""

import threading
from datetime import timedelta

import pytest
from django.db import connections
from django.utils import timezone

from apps.common.platforms import Platform
from apps.flows.models import DefaultReplyState, HandledComment
from apps.flows.tests.support import connection_for, contact_for
from apps.flows.triggers.guards import (
    DEFAULT_REPLY_INTERVAL,
    PRIVATE_REPLY_WINDOW,
    claim_default_reply,
    mark_private_reply_sent,
    may_claim_comment,
    may_private_reply,
    private_reply_deadline,
    record_comment,
)


def _in_threads(work, count):
    """Release `count` threads together and collect what each returned.

    The house shape: a real Barrier so the threads genuinely race, and
    ``connections.close_all()`` in a ``finally`` in every worker, because a
    ``transaction=True`` test leaves each thread holding its own connection.
    """
    barrier = threading.Barrier(count)
    results: list[object] = []
    lock = threading.Lock()

    def run(index):
        try:
            barrier.wait(timeout=10)
            outcome = work(index)
            with lock:
                results.append(outcome)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return results


@pytest.mark.django_db
class TestDefaultReplyGuard:
    def test_the_first_claim_is_granted(self, tenancy, connection, contact):
        assert claim_default_reply(contact, connection) is True

    def test_a_second_claim_inside_the_window_is_refused(self, tenancy, connection, contact):
        claim_default_reply(contact, connection)
        assert claim_default_reply(contact, connection) is False

    def test_a_claim_after_the_window_is_granted(self, tenancy, connection, contact):
        claim_default_reply(contact, connection)
        DefaultReplyState.objects.for_workspace(tenancy.workspace).update(
            last_sent_at=timezone.now() - DEFAULT_REPLY_INTERVAL - timedelta(minutes=1)
        )

        assert claim_default_reply(contact, connection) is True

    def test_the_window_is_rolling_not_clock_aligned(self, tenancy, connection, contact):
        """The exact case ``apps.common.ratelimit``'s aligned buckets let through.

        Its key carries ``floor(now / 86400)``, so a reply at 23:59:59 and
        another at 00:00:01 land in different buckets and both pass — two
        identical fallbacks two seconds apart, which is the complaint the guard
        exists to prevent. Measured from the last send, they cannot.
        """
        from datetime import UTC, datetime

        midnight_eve = datetime(2026, 8, 22, 23, 59, 59, tzinfo=UTC)
        just_after = datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC)

        assert claim_default_reply(contact, connection, now=midnight_eve) is True
        assert claim_default_reply(contact, connection, now=just_after) is False

    def test_the_window_is_measured_from_the_last_send(self, tenancy, connection, contact):
        """A refused attempt does not push the window out — otherwise a chatty
        contact could never be answered again."""
        now = timezone.now()
        assert claim_default_reply(contact, connection, now=now) is True
        assert claim_default_reply(contact, connection, now=now + timedelta(hours=23)) is False
        # Exactly one interval after the send, a reply is due again.
        assert claim_default_reply(contact, connection, now=now + DEFAULT_REPLY_INTERVAL) is True

    def test_it_is_per_channel(self, tenancy, connection, contact):
        second = connection_for(tenancy.workspace, external_id="bot-2")
        claim_default_reply(contact, connection)

        assert claim_default_reply(contact, second) is True

    def test_the_row_is_readable(self, tenancy, connection, contact):
        """An operator asking "why did this person get two" needs something to
        look at — which a SHA-256 rate-limit key is not."""
        claim_default_reply(contact, connection)
        row = DefaultReplyState.objects.for_workspace(tenancy.workspace).get()

        assert row.contact_id == contact.pk
        assert row.channel_connection_id == connection.pk


@pytest.mark.django_db(transaction=True)
class TestDefaultReplyGuardUnderConcurrency:
    def test_exactly_one_of_two_simultaneous_claims_wins(self, tenancy):
        connection = connection_for(tenancy.workspace, external_id="bot-race")
        contact = contact_for(tenancy.workspace)

        try:
            results = _in_threads(lambda index: claim_default_reply(contact, connection), 2)
            assert sorted(results) == [False, True]
        finally:
            DefaultReplyState.objects.unscoped().filter(contact=contact).delete()  # transaction=True cleanup


@pytest.mark.django_db
class TestCommentGuard:
    def _comment(self, connection, **overrides):
        fields = {
            "connection": connection,
            "trigger": None,
            "comment_id": "c-1",
            "post_id": "p-1",
            "commenter_ref": "ig-1",
            "commented_at": timezone.now(),
        }
        fields.update(overrides)
        return record_comment(**fields)

    @pytest.fixture
    def instagram(self, tenancy):
        return connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-acme")

    def test_the_first_comment_is_claimed(self, tenancy, instagram):
        assert self._comment(instagram) is not None

    def test_a_redelivered_comment_is_refused(self, tenancy, instagram):
        self._comment(instagram)
        assert self._comment(instagram) is None

    def test_a_second_comment_from_the_same_person_on_the_same_post_is_refused(self, tenancy, instagram):
        self._comment(instagram, comment_id="c-1")
        assert self._comment(instagram, comment_id="c-2") is None

    def test_a_second_comment_is_allowed_when_the_trigger_says_so(self, tenancy, instagram):
        self._comment(instagram, comment_id="c-1", once_per_contact_per_post=False)
        assert self._comment(instagram, comment_id="c-2", once_per_contact_per_post=False) is not None

    def test_a_different_person_on_the_same_post_is_allowed(self, tenancy, instagram):
        self._comment(instagram, comment_id="c-1", commenter_ref="ig-1")
        assert self._comment(instagram, comment_id="c-2", commenter_ref="ig-2") is not None

    def test_two_overlong_comment_ids_sharing_a_prefix_are_distinct(self, tenancy, instagram):
        """These values go straight into unique constraints, so truncating them
        would make the second comment look already handled and silently drop its
        private reply."""
        assert self._comment(instagram, comment_id="c" * 250 + "A") is not None
        assert self._comment(instagram, comment_id="c" * 250 + "B", commenter_ref="ig-2") is not None

    def test_two_overlong_commenters_sharing_a_prefix_both_get_a_claim(self, tenancy, instagram):
        assert self._comment(instagram, comment_id="c-1", commenter_ref="ig-" + "z" * 250 + "A") is not None
        assert self._comment(instagram, comment_id="c-2", commenter_ref="ig-" + "z" * 250 + "B") is not None

    def test_a_future_timestamp_is_clamped(self, tenancy, instagram):
        """A forged timestamp must not buy extra days inside the 7-day window."""
        now = timezone.now()
        row = self._comment(instagram, commented_at=now + timedelta(days=30), now=now)

        assert row.commented_at <= now

    def test_the_deadline_is_checked_before_the_claim_is_spent(self, tenancy):
        """Claiming a comment past the window would spend that person's one
        claim on that post in exchange for a reply the platform refuses."""
        now = timezone.now()

        assert may_claim_comment(now) is True
        assert may_claim_comment(now - PRIVATE_REPLY_WINDOW + timedelta(hours=1), now=now) is True
        assert may_claim_comment(now - PRIVATE_REPLY_WINDOW - timedelta(hours=1), now=now) is False

    def test_a_future_comment_cannot_extend_its_own_window(self, tenancy):
        """The timestamp is clamped on both sides, so a forged date can move a
        comment out of the window but never further into it."""
        now = timezone.now()

        assert may_claim_comment(now + timedelta(days=30), now=now) is True

    def test_the_deadline_is_seven_days_out(self, tenancy, instagram):
        row = self._comment(instagram)
        assert private_reply_deadline(row) == row.commented_at + PRIVATE_REPLY_WINDOW

    def test_a_reply_is_refused_past_the_deadline(self, tenancy, instagram):
        row = self._comment(instagram)
        assert may_private_reply(row) is True
        assert may_private_reply(row, now=timezone.now() + PRIVATE_REPLY_WINDOW + timedelta(minutes=1)) is False

    def test_a_reply_is_refused_once_it_has_been_sent(self, tenancy, instagram, contact):
        row = self._comment(instagram)
        mark_private_reply_sent(row, contact=contact)

        assert may_private_reply(row) is False
        row.refresh_from_db()
        assert row.contact_id == contact.pk

    def test_the_callers_transaction_survives_a_refusal(self, tenancy, instagram):
        """The inner atomic() is load-bearing: without it the IntegrityError
        poisons the caller's transaction rather than being handled."""
        from django.db import transaction

        with transaction.atomic():
            self._comment(instagram, comment_id="c-1")
            assert self._comment(instagram, comment_id="c-2") is None
            # Still usable — which is the whole assertion.
            assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 1


@pytest.mark.django_db(transaction=True)
class TestCommentGuardUnderConcurrency:
    def test_two_simultaneous_comments_yield_one_row(self, tenancy):
        """SPEC §10's once-per-contact-per-post, under the race that actually
        happens: two comments from one person arrive in a single Meta batch and
        are processed in separate transactions, so a SELECT-then-INSERT would let
        both through every time."""
        connection = connection_for(tenancy.workspace, platform=Platform.INSTAGRAM, external_id="ig-race")

        try:
            results = _in_threads(
                lambda index: record_comment(
                    connection=connection,
                    trigger=None,
                    comment_id=f"c-{index}",
                    post_id="p-1",
                    commenter_ref="ig-1",
                    commented_at=timezone.now(),
                ),
                4,
            )
            claimed = [row for row in results if row is not None]
            assert len(claimed) == 1
            assert HandledComment.objects.unscoped().filter(channel_connection=connection).count() == 1
        finally:
            HandledComment.objects.unscoped().filter(channel_connection=connection).delete()  # transaction=True
