"""Comment to DM, end to end — SPEC §10's comment trigger on Messenger.

    public reply and like are executed via the platform API, the private reply
    (the flow's first message) counts against the one-private-reply-per-comment
    rule; store handled comment ids to enforce once_per_contact_per_post and the
    7-day private-reply deadline.

L4-A built every platform-agnostic part of that: ``TriggerType.COMMENT``, the
matcher, ``HandledComment`` and the two guards. What #18 adds is a parser, a
registration on ``apps.flows.triggers.comments``, and the worker that answers.

--------------------------------------------------------------------------
The one subtle thing, and why it is a test rather than a comment
--------------------------------------------------------------------------

A comment creates **no contact** — ``apps.messaging.ingest`` says so at length,
because one viral post would otherwise be a contact-spam amplifier. So before the
trigger's flow can run there has to be an identity, and before its first message
can go out there has to be an open messaging window. Both of those are written by
exactly one place in the project (ROADMAP contract 3, enforced by an AST scan),
and that place is ``persist_events`` applying an inbound event.

So the comment→DM transition is expressed as the event it actually is: this
person has started a conversation. ``TestTheWindowIsOpenedByTheOneWriteSite``
below is what holds that — if a future change opens the window some other way,
this is the test that should stop it.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels.models import ChannelConnection
from apps.channels.providers import messenger as messenger_adapter
from apps.channels.tests.messenger_support import PSID, fake_graph, load_delivery, post_webhook
from apps.flows.models import Flow, FlowExecution, HandledComment, Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow
from apps.flows.triggers.services import create_trigger
from apps.messaging.models import ContactChannelIdentity, Message, MessageDirection
from apps.queueing.models import ScheduledAction
from apps.queueing.worker import process_action
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

PRIVATE_REPLY = "Thanks for asking! It is £40 — shall I send you the link?"


@pytest.fixture(autouse=True)
def real_pipeline() -> None:
    """The real contract-6 processors. See ``test_messenger_e2e.real_pipeline``."""
    from apps.channels import ingest as channels_ingest
    from apps.flows.triggers.pipeline import ROUTING_PROCESSOR, route_events
    from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)


@pytest.fixture
def comment_flow(tenancy: Tenancy) -> Flow:
    return published_flow(
        tenancy.workspace,
        graph([node("start", "send_message", {"blocks": [{"type": "text", "text": PRIVATE_REPLY}]})]),
        name="Comment to DM",
    )


@pytest.fixture
def comment_trigger(comment_flow: Flow, page: ChannelConnection) -> Trigger:
    return create_trigger(
        comment_flow,
        trigger_type=TriggerType.COMMENT,
        config={
            "post_scope": "all",
            "post_ids": [],
            "include_keywords": [],
            "exclude_keywords": [],
            "top_level_only": True,
            "once_per_contact_per_post": True,
            "like_comment": True,
            "public_reply": {"mode": "static", "texts": ["Sent you a DM!"]},
        },
        connection=page,
    )


def deliver_comment(client: Client, *, comment_id: str = "", message: str = "") -> Any:
    """POST the recorded comment delivery, stamped with a current ``created_time``.

    The fixture holds a fixed moment so its *shape* is a real one, but SPEC §10's
    private-reply deadline is measured from that field — so a recorded payload
    replayed a year later is, correctly, a comment too old to answer. Restamping
    is what keeps these tests about the comment path rather than about the clock;
    the deadline itself has its own test, which sets the timestamp deliberately.
    """
    payload = load_delivery("feed_comment")
    value = payload["entry"][0]["changes"][0]["value"]
    value["created_time"] = int(timezone.now().timestamp())
    if comment_id:
        value["comment_id"] = comment_id
    if message:
        value["message"] = message
    with fake_graph() as calls:
        response = post_webhook(client, payload)
    return response, calls


def run_queued_actions() -> Any:
    """Drain the comment follow-up the webhook enqueued.

    Through ``apps.queueing.worker.process_action`` rather than by calling the
    handler, so the queue's own claiming, retry and idempotency behaviour is in
    the path — the same reasoning ``apps/flows/tests/test_handlers.py`` gives.
    """
    with fake_graph() as calls:
        for action in ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION):
            process_action(action)
    return calls


class TestTheClaim:
    def test_a_comment_is_claimed_and_a_follow_up_is_enqueued(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        response, inline = deliver_comment(client)
        assert response.status_code == 200

        row = HandledComment.objects.unscoped().get()
        assert row.trigger_id == comment_trigger.pk
        assert row.commenter_ref == PSID
        assert row.contact_id is None  # no contact until the DM opens one

        assert ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION).count() == 1
        # Nothing was called inline: three Graph round trips would blow SPEC
        # §7.1's 1.5 s budget for the whole webhook path.
        assert inline.calls == []

    def test_a_redelivery_claims_nothing_further(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        deliver_comment(client)
        deliver_comment(client)
        assert HandledComment.objects.unscoped().count() == 1
        assert ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION).count() == 1

    def test_a_comment_that_does_not_match_the_keywords_claims_nothing(
        self, client: Client, tenancy: Tenancy, comment_flow: Flow, page: ChannelConnection
    ) -> None:
        create_trigger(
            comment_flow,
            trigger_type=TriggerType.COMMENT,
            config={"post_scope": "all", "include_keywords": ["price"], "top_level_only": True},
            connection=page,
        )
        deliver_comment(client, message="lovely photo")
        assert not HandledComment.objects.unscoped().exists()

    def test_a_comment_past_the_seven_day_deadline_is_never_claimed(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """Claiming would spend the once-per-post guard on a reply Meta refuses."""
        payload = load_delivery("feed_comment")
        eight_days_ago = timezone.now() - timedelta(days=8)
        payload["entry"][0]["changes"][0]["value"]["created_time"] = int(eight_days_ago.timestamp())
        with fake_graph():
            post_webhook(client, payload)
        assert not HandledComment.objects.unscoped().exists()

    def test_a_comment_with_no_trigger_claims_nothing(self, client: Client, page: ChannelConnection) -> None:
        deliver_comment(client)
        assert not HandledComment.objects.unscoped().exists()
        assert not ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION).exists()


class TestTheFollowUp:
    def test_the_public_reply_and_the_like_go_out(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        deliver_comment(client)
        calls = run_queued_actions()
        row = HandledComment.objects.unscoped().get()

        assert calls.bodies(f"/{row.comment_id}/comments") == [{"message": "Sent you a DM!"}]
        assert any(call.matches(f"/{row.comment_id}/likes") for call in calls.calls)

    def test_neither_is_sent_when_the_trigger_does_not_ask_for_them(
        self, client: Client, tenancy: Tenancy, comment_flow: Flow, page: ChannelConnection
    ) -> None:
        create_trigger(
            comment_flow,
            trigger_type=TriggerType.COMMENT,
            config={"post_scope": "all", "top_level_only": True, "public_reply": {"mode": "none"}},
            connection=page,
        )
        deliver_comment(client)
        calls = run_queued_actions()
        assert not any("/comments" in call.path or "/likes" in call.path for call in calls.calls)

    def test_a_failed_public_reply_does_not_cost_the_private_one(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """The private reply is the half that actually starts the conversation."""
        from apps.channels.tests.messenger_support import Reply

        deliver_comment(client)
        with fake_graph() as calls:
            calls.reply("/comments", Reply(status=500))
            for action in ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION):
                process_action(action)
        assert calls.bodies("/messages")


class TestThePrivateReplyIsTheFlowsFirstMessage:
    def test_it_is_addressed_by_comment_id_and_carries_the_flows_text(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """SPEC §10, literally: Meta allows exactly one message in reply to a comment.

        So an opener followed by the flow's real first message would have the
        second one refused — the flow's first message has to *be* the private
        reply, and the adapter addresses it that way.
        """
        deliver_comment(client)
        calls = run_queued_actions()
        row = HandledComment.objects.unscoped().get()

        (body,) = calls.bodies("/messages")
        assert body["recipient"] == {"comment_id": row.comment_id}
        assert body["message"]["text"] == PRIVATE_REPLY

    def test_the_flow_really_ran(self, client: Client, page: ChannelConnection, comment_trigger: Trigger) -> None:
        deliver_comment(client)
        run_queued_actions()
        execution = FlowExecution.objects.unscoped().get()
        assert execution.flow_version.flow_id == comment_trigger.flow_id

    def test_the_guard_is_spent_and_the_contact_recorded(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        deliver_comment(client)
        run_queued_actions()
        row = HandledComment.objects.unscoped().get()
        assert row.private_reply_sent_at is not None
        assert row.contact_id is not None

    def test_a_second_comment_from_the_same_person_on_the_same_post_sends_no_second_reply(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """``once_per_contact_per_post``, arbitrated by the database rather than a read."""
        deliver_comment(client)
        run_queued_actions()
        first_count = Message.objects.unscoped().filter(direction=MessageDirection.OUT).count()

        deliver_comment(client, comment_id="111111111111111_9099", message="still interested?")
        calls = run_queued_actions()
        assert calls.bodies("/messages") == []
        assert Message.objects.unscoped().filter(direction=MessageDirection.OUT).count() == first_count

    def test_running_the_action_twice_sends_one_private_reply(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """A retried queue row must not answer the same comment twice."""
        deliver_comment(client)
        run_queued_actions()
        again = run_queued_actions()
        assert again.bodies("/messages") == []


class TestTheWindowIsOpenedByTheOneWriteSite:
    def test_the_dm_thread_gets_an_identity_with_an_open_window(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """Contract 3 gives ``window_expires_at`` exactly one writer.

        The comment→DM transition therefore goes through it, as the inbound event
        it genuinely is, rather than by assigning the column from the worker.
        """
        deliver_comment(client)
        run_queued_actions()

        identity = ContactChannelIdentity.objects.unscoped().get(platform_user_id=PSID)
        assert identity.opt_in is True
        assert identity.window_expires_at is not None
        assert identity.window_expires_at > timezone.now()

    def test_the_comment_itself_writes_no_thread_row(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """A comment is public, not a DM.

        The only inbound-side row this whole path creates is the identity: the
        opener is a ``referral``, which ``apps.messaging.ingest``'s own table says
        creates an identity and a window and no message.
        """
        deliver_comment(client)
        run_queued_actions()
        assert not Message.objects.unscoped().filter(direction=MessageDirection.IN).exists()

    def test_the_comments_own_text_never_fires_a_keyword_trigger(
        self, client: Client, tenancy: Tenancy, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """The reason the opener is persisted rather than routed.

        ``persist_events`` rather than ``process_events``: pushing the synthetic
        opener round the routing stages would let the comment's text match a
        keyword trigger on top of the comment trigger that already matched it, and
        the person would get two flows for one comment.
        """
        keyword_flow = published_flow(
            tenancy.workspace,
            graph([node("start", "send_message", {"blocks": [{"type": "text", "text": "Keyword flow!"}]})]),
            name="Keyword",
        )
        create_trigger(
            keyword_flow,
            trigger_type=TriggerType.KEYWORD,
            config={"keywords": [{"text": "how much", "mode": "contains"}]},
            connection=page,
        )

        deliver_comment(client)
        calls = run_queued_actions()
        texts = [body["message"].get("text") for body in calls.bodies("/messages")]
        assert texts == [PRIVATE_REPLY]


class TestTheSeamIsPlatformAgnostic:
    def test_the_registry_is_what_dispatches(self) -> None:
        """L5-A adds one ``register_comment_actions`` line, not an edit to stages."""
        from apps.flows.triggers import comments

        assert comments.actions_for("messenger") is messenger_adapter.enqueue_comment_actions
        assert comments.actions_for("instagram") is None

    def test_a_platform_with_nothing_registered_still_claims_the_comment(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """The behaviour before any Layer-5 adapter shipped: claimed, unanswered."""
        from apps.flows.triggers import comments

        registered = comments._ACTIONS.pop("messenger")
        try:
            deliver_comment(client)
        finally:
            comments._ACTIONS["messenger"] = registered

        assert HandledComment.objects.unscoped().count() == 1
        assert not ScheduledAction.objects.unscoped().filter(type=messenger_adapter.COMMENT_ACTION).exists()

    def test_a_failing_actions_callable_never_rolls_back_the_claim(
        self, client: Client, page: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """The failure mode ``run_actions`` swallows exceptions to prevent.

        A raise here would be caught by ``hooks._run_one``, reported as a failed
        hook and rolled back with the savepoint — un-recording the comment, so the
        next redelivery would claim it again and the guard would never hold.
        """
        from apps.flows.triggers import comments

        def explode(claim: Any) -> None:
            raise RuntimeError("Meta is on fire")

        registered = comments._ACTIONS["messenger"]
        comments._ACTIONS["messenger"] = explode
        try:
            response, _calls = deliver_comment(client)
        finally:
            comments._ACTIONS["messenger"] = registered

        assert response.status_code == 200
        assert HandledComment.objects.unscoped().count() == 1

    def test_the_extra_keys_this_adapter_writes_are_the_ones_l4a_fixed(self) -> None:
        """The literals in ``providers.messenger`` against their source of truth.

        They are duplicated rather than imported so ``apps.channels`` keeps no
        module-scope dependency on ``apps.flows`` — the same trade
        ``apps.flows.triggers.pipeline.ROUTING_PROCESSOR`` makes in the other
        direction. This is the test that keeps the duplication honest.
        """
        from apps.flows.triggers import types

        assert messenger_adapter.COMMENT_POST_ID_KEY == types.COMMENT_POST_ID_KEY
        assert messenger_adapter.COMMENT_PARENT_ID_KEY == types.COMMENT_PARENT_ID_KEY
        assert messenger_adapter.COMMENT_TEXT_KEY == types.COMMENT_TEXT_KEY
        assert messenger_adapter.MAX_REF_CHARS == types.MAX_REF_CHARS
