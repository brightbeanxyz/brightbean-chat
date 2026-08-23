"""Comment to DM, end to end — SPEC §10's headline Instagram feature.

    Comment trigger behavior: public reply and like are executed via the platform
    API, the private reply (the flow's first message) counts against the
    one-private-reply-per-comment rule; store handled comment ids to enforce
    once_per_contact_per_post and the 7-day private-reply deadline.

Everything between the two ends is the production path: the webhook endpoint,
signature verification, deduplication, the contract-6 seam, L3-A's persistence
and compliance and token bucket, L4-A's routing stages and comment guard, L3-B's
engine, and this adapter. The only substitution is the network.

The four assertions the issue names are :class:`TestCommentToDm` below; the ones
about *not* doing something are the ones worth reading twice.
"""

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels.models import ChannelConnection
from apps.channels.providers.instagram import COMMENT_REPLY_ACTION
from apps.channels.providers.meta_common import SIGNATURE_HEADER
from apps.channels.tests.instagram_support import IG_USER_ID, at_now, fake_graph, load_delivery, sign
from apps.flows.models import Flow, FlowExecution, HandledComment, Trigger, TriggerType
from apps.flows.tests.support import graph as flow_graph
from apps.flows.tests.support import node
from apps.flows.triggers import comments as comment_responders
from apps.messaging.models import ContactChannelIdentity, Message, MessageDirection, OptInSource
from apps.queueing.models import ScheduledAction
from apps.queueing.registry import get_handler
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/instagram/"
COMMENT_ID = "17900000000000101"
POST_ID = "17800000000000200"

REPLY_FLOW = flow_graph(
    [node("say", "send_message", {"blocks": [{"type": "text", "text": "It is 40 EUR, shipped."}]})],
    [],
)


@pytest.fixture
def real_pipeline() -> Iterator[None]:
    """The production contract-6 seam, which this app's conftest clears by default.

    ``_clean_processors`` empties the registry so the framework's own tests are
    about the seam rather than about whatever apps are installed, and restores it
    afterwards — so registering the real stages here is safe and does not leak.
    """
    from apps.flows.triggers.pipeline import register_routing
    from apps.messaging.ingest import register_processors

    register_processors()
    register_routing()
    yield


@pytest.fixture
def comment_flow(tenancy: Tenancy) -> Flow:
    from apps.flows.services import create_flow, publish, save_draft

    flow = create_flow(workspace=tenancy.workspace, name="Price replies")
    save_draft(flow, REPLY_FLOW)
    publish(flow)
    flow.refresh_from_db()
    return flow


@pytest.fixture
def comment_trigger(comment_flow: Flow, instagram_connection: ChannelConnection) -> Trigger:
    trigger = Trigger(
        flow=comment_flow,
        channel_connection=instagram_connection,
        type=TriggerType.COMMENT,
        config_json={
            "post_scope": "all",
            "post_ids": [],
            "include_keywords": ["price"],
            "exclude_keywords": [],
            "top_level_only": True,
            "public_reply": {"mode": "static", "texts": ["Sent you a DM!"]},
            "like_comment": True,
            "once_per_contact_per_post": True,
        },
    )
    trigger.save()
    return trigger


def deliver(client: Client, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode()
    return client.post(
        WEBHOOK_URL,
        data=body,
        content_type="application/json",
        headers={SIGNATURE_HEADER: sign(body)},
    )


def run_queued(action_type: str = COMMENT_REPLY_ACTION) -> int:
    """Drain the queued comment replies the way the worker would."""
    handler = get_handler(action_type)
    assert handler is not None, "the adapter module registers it on import"
    rows = list(ScheduledAction.objects.unscoped().filter(type=action_type))
    for row in rows:
        handler(row.payload, row)
    return len(rows)


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestCommentToDm:
    def test_keyword_match_sends_a_public_reply_and_a_private_reply(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        with fake_graph() as api:
            assert deliver(client, at_now(load_delivery("comment"))).status_code == 200
            # The claim is taken inline; the two round trips are not (SPEC §7.1
            # budgets 1.5 s for the whole inline path).
            assert api.calls == []
            assert run_queued() == 1

        # 1. The public reply, under the comment.
        assert api.bodies(f"{COMMENT_ID}/replies") == [{"message": "Sent you a DM!"}]

        # 2. The private reply, addressed by comment id rather than by user id —
        #    Meta will not accept an ordinary DM to somebody who has never
        #    messaged the account.
        (message,) = api.message_bodies()
        assert message["recipient"] == {"comment_id": COMMENT_ID}
        assert message["message"]["text"] == "It is 40 EUR, shipped."

        # 3. The DM thread now exists, with consent recorded as what it was.
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.platform_user_id == IG_USER_ID
        assert identity.opt_in is True
        assert identity.opt_in_source == OptInSource.COMMENT
        # The compliance window is what lets the reply through the chokepoint at
        # all: SPEC §8 gates every send on it, comment or not.
        assert identity.window_expires_at is not None

        # 4. The guard is spent, and it names the contact the thread belongs to.
        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.private_reply_sent_at is not None
        assert row.contact_id == identity.contact_id

    def test_a_second_comment_from_the_same_person_gets_no_second_reply(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """SPEC §10's ``once_per_contact_per_post``, enforced by the database
        rather than by a read followed by a write."""
        second = at_now(load_delivery("comment"))
        second["entry"][0]["changes"][0]["value"]["id"] = "17900000000000199"
        second["entry"][0]["changes"][0]["value"]["text"] = "price again?"

        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            deliver(client, second)
            run_queued()

        assert len(api.message_bodies()) == 1
        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_redelivered_comment_is_a_no_op(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Every platform retries. The event log's unique constraint catches the
        first line and ``HandledComment``'s catches the second."""
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            deliver(client, at_now(load_delivery("comment")))
            run_queued()

        assert len(api.message_bodies()) == 1
        assert len(api.bodies(f"{COMMENT_ID}/replies")) == 1

    def test_a_comment_past_the_seven_day_deadline_is_never_claimed(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Claiming would burn the one claim that person had on that post, in
        exchange for a reply Meta will refuse."""
        old = at_now(load_delivery("comment"), offset_days=8)

        with fake_graph() as api:
            assert deliver(client, old).status_code == 200
            assert run_queued() == 0

        assert api.calls == []
        assert not HandledComment.objects.for_workspace(tenancy.workspace).exists()

    def test_a_reply_queued_before_the_deadline_is_refused_after_it(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """The queue is durable and the deadline is not ours. A row that waited
        out the window must not send."""
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            row = HandledComment.objects.for_workspace(tenancy.workspace).get()
            HandledComment.objects.for_workspace(tenancy.workspace).filter(pk=row.pk).update(
                commented_at=timezone.now() - timedelta(days=8)
            )
            run_queued()

        assert api.calls == []

    def test_like_comment_is_configured_and_never_called(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Meta's IG Comment reference exposes ``like_count`` as read-only and
        only ``hide`` and ``replies`` as write operations — there is no way to
        like a comment through this API. The config is honoured as a no-op and
        the option is hidden in the trigger form; see docs/channels/instagram.md.
        """
        assert comment_trigger.config_json["like_comment"] is True
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert not any("like" in path for path in api.paths())

    def test_the_instagram_responder_declares_no_like_support(self) -> None:
        responder = comment_responders.responder_for("instagram")
        assert responder is not None
        assert responder.supports_like is False
        assert comment_responders.like_supported_on(["instagram"]) is False


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestACommenterWeAlreadyKnow:
    """The routing gate used to branch on whether a contact existed, not on
    whether this was a comment — so anyone who had ever sent a DM bypassed SPEC
    §10's guards entirely and got no public reply."""

    def _with_a_dm_thread(self, client: Client, tenancy: Tenancy) -> Any:
        with fake_graph():
            deliver(client, at_now(load_delivery("message_text")))
        return ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact

    def test_their_comment_is_claimed(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        contact = self._with_a_dm_thread(client, tenancy)
        with fake_graph():
            deliver(client, at_now(load_delivery("comment")))
            run_queued()

        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.comment_id == COMMENT_ID
        assert row.commenter_ref == IG_USER_ID
        assert contact is not None

    def test_they_get_the_public_reply(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """An author who configures one wants it for repeat customers too."""
        self._with_a_dm_thread(client, tenancy)
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert api.bodies(f"{COMMENT_ID}/replies") == [{"message": "Sent you a DM!"}]

    def test_the_flow_runs_exactly_once(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Started synchronously by the routing stage. The queued half skips its
        re-dispatch for a known commenter, which would otherwise start it again.
        """
        self._with_a_dm_thread(client, tenancy)
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()

        replies = [body for body in api.message_bodies() if body["message"].get("text") == "It is 40 EUR, shipped."]
        assert len(replies) == 1
        # An ordinary DM: the thread is already open, so nothing is a private reply.
        assert replies[0]["recipient"] == {"id": IG_USER_ID}
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_their_second_comment_on_the_post_is_refused(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """``once_per_contact_per_post`` — the setting that silently did nothing
        for this class of commenter."""
        self._with_a_dm_thread(client, tenancy)
        second = at_now(load_delivery("comment"))
        second["entry"][0]["changes"][0]["value"]["id"] = "17900000000000177"
        second["entry"][0]["changes"][0]["value"]["text"] = "price again?"

        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            deliver(client, second)
            run_queued()

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 1
        assert len(api.bodies(f"{COMMENT_ID}/replies")) == 1
        replies = [body for body in api.message_bodies() if body["message"].get("text") == "It is 40 EUR, shipped."]
        assert len(replies) == 1

    def test_the_setting_still_lets_a_second_comment_through(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        comment_trigger.config_json["once_per_contact_per_post"] = False
        comment_trigger.save(update_fields=["config_json", "updated_at"])
        self._with_a_dm_thread(client, tenancy)
        second = at_now(load_delivery("comment"))
        second["entry"][0]["changes"][0]["value"]["id"] = "17900000000000177"

        with fake_graph():
            deliver(client, at_now(load_delivery("comment")))
            deliver(client, second)
            run_queued()

        assert HandledComment.objects.for_workspace(tenancy.workspace).count() == 2


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestPublicReply:
    def test_no_public_reply_when_the_mode_is_none(
        self, client: Client, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        comment_trigger.config_json["public_reply"] = {"mode": "none", "texts": ["unused"]}
        comment_trigger.save(update_fields=["config_json", "updated_at"])
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert api.bodies(f"{COMMENT_ID}/replies") == []
        # The private reply still goes out: it is the thing the flow is for.
        assert len(api.message_bodies()) == 1

    def test_a_random_public_reply_comes_from_the_list(
        self, client: Client, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        comment_trigger.config_json["public_reply"] = {"mode": "random", "texts": ["one", "two", "three"]}
        comment_trigger.save(update_fields=["config_json", "updated_at"])
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        (body,) = api.bodies(f"{COMMENT_ID}/replies")
        assert body["message"] in {"one", "two", "three"}

    def test_a_failed_public_reply_does_not_cost_the_private_one(
        self, client: Client, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        """The private reply has a seven-day clock on it and is the thing the
        author actually configured a flow for."""
        from apps.channels.tests.instagram_support import Reply

        with fake_graph(lambda api: api.reply(f"{COMMENT_ID}/replies", Reply(status=400))) as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert len(api.message_bodies()) == 1


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestMatching:
    def test_a_comment_that_misses_the_keywords_does_nothing(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        payload = at_now(load_delivery("comment"))
        payload["entry"][0]["changes"][0]["value"]["text"] = "nice photo"
        with fake_graph() as api:
            deliver(client, payload)
            run_queued()
        assert api.calls == []
        assert not HandledComment.objects.for_workspace(tenancy.workspace).exists()

    def test_a_reply_to_another_comment_is_ignored_when_top_level_only(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        payload = at_now(load_delivery("comment_reply"))
        payload["entry"][0]["changes"][0]["value"]["text"] = "price?"
        with fake_graph() as api:
            deliver(client, payload)
            run_queued()
        assert api.calls == []

    def test_a_specific_post_scope_is_honoured(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, comment_trigger: Trigger
    ) -> None:
        comment_trigger.config_json.update({"post_scope": "specific", "post_ids": ["some-other-post"]})
        comment_trigger.save(update_fields=["config_json", "updated_at"])
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert api.calls == []

        comment_trigger.config_json["post_ids"] = [POST_ID]
        comment_trigger.save(update_fields=["config_json", "updated_at"])
        # A *different* comment: redelivering the first one is refused one layer
        # up by the event log's unique (connection, provider_event_id), which
        # would make this pass for the wrong reason.
        second = at_now(load_delivery("comment"))
        second["entry"][0]["changes"][0]["value"]["id"] = "17900000000000188"
        with fake_graph() as api:
            deliver(client, second)
            run_queued()
        assert len(api.message_bodies()) == 1

    def test_a_claimed_comment_opens_no_empty_thread(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
        comment_flow: Flow,
    ) -> None:
        """A comment is not a DM thread. The thread is opened by the private
        reply, through ``services.send_outbound`` — so a claim whose reply never
        sends leaves nothing at the top of somebody's inbox.
        """
        # A flow with no publishable version: the trigger is not a candidate, so
        # nothing is claimed. Claim it directly instead, then re-dispatch it the
        # way the queue handler does, and stop before the flow sends anything.
        from apps.channels.providers.instagram import _open_thread
        from apps.flows.triggers import guards
        from apps.messaging.models import Conversation

        row = guards.record_comment(
            connection=instagram_connection,
            trigger=comment_trigger,
            comment_id=COMMENT_ID,
            post_id=POST_ID,
            commenter_ref=IG_USER_ID,
            commented_at=timezone.now(),
        )
        assert row is not None
        comment_flow.status = "draft"
        comment_flow.save(update_fields=["status", "updated_at"])

        with fake_graph():
            _open_thread(instagram_connection, row, {"comment_text": "PRICE please"})

        # The contact, the consent record and the window all exist — the private
        # reply needs every one of them to clear SPEC §8's chokepoint.
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is not None
        # The thread does not.
        assert not Conversation.objects.for_workspace(tenancy.workspace).exists()

    def test_a_comment_creates_no_contact_until_it_is_claimed(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """No trigger, so no claim — and one viral post must not become a
        contact-spam amplifier (``apps.messaging.ingest``)."""
        with fake_graph():
            deliver(client, at_now(load_delivery("comment")))
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestAfterTheThreadOpens:
    def test_the_next_message_goes_to_the_person_not_the_comment(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Meta allows exactly one private reply per comment. Everything after it
        is an ordinary DM, which works because the thread is now open."""
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
            # The contact answers, and the flow's channel is now a real thread.
            deliver(client, at_now(load_delivery("message_text")))

        contact = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        inbound = Message.objects.for_workspace(tenancy.workspace).filter(direction=MessageDirection.IN)
        assert inbound.count() == 1

        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging import services

        with fake_graph() as api:
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=instagram_connection,
                outbound=OutboundMessage(blocks=(TextBlock(text="Anything else?"),)),
                source="automation",
                idempotency_key="test:followup",
            )
        (body,) = api.message_bodies()
        assert body["recipient"] == {"id": IG_USER_ID}


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestAMultiPartPrivateReply:
    def test_only_the_first_message_is_addressed_to_the_comment(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_flow: Flow,
        comment_trigger: Trigger,
    ) -> None:
        """Meta allows exactly one private reply per comment. A flow whose first
        node is a gallery, or whose text is longer than Instagram's cap, is
        several sends — and the rest have to go to the person, which works
        because the private reply just opened the thread."""
        from apps.flows.services import publish, save_draft

        long_flow = flow_graph(
            [
                node(
                    "say",
                    "send_message",
                    {"blocks": [{"type": "text", "text": "word " * 400}]},
                )
            ],
            [],
        )
        save_draft(comment_flow, long_flow)
        publish(comment_flow)

        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()

        recipients = [body["recipient"] for body in api.message_bodies()]
        assert len(recipients) > 1
        assert recipients[0] == {"comment_id": COMMENT_ID}
        assert all(item == {"id": IG_USER_ID} for item in recipients[1:])

        # And the one private reply this comment gets is spent exactly once.
        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.private_reply_sent_at is not None


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestTheClaimIsNotAGeneralLicence:
    """A stale claim must not hijack an unrelated send. See ``_pending_private_reply``."""

    def test_a_later_send_on_an_open_thread_is_an_ordinary_dm(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """The claim is spent by the message that opens the thread, and the row
        is marked — so nothing afterwards can be readdressed."""
        with fake_graph():
            deliver(client, at_now(load_delivery("comment")))
            run_queued()

        contact = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get().contact
        with fake_graph() as api:
            _send(tenancy, contact, instagram_connection, "Anything else?", source="agent")
        assert api.message_bodies()[0]["recipient"] == {"id": IG_USER_ID}

    def test_an_unspent_claim_does_not_convert_a_send_to_someone_who_wrote_first(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """The case the guard was missing.

        An unanswered claim is not on its own a licence to readdress a send.
        Here one exists — the private reply never went out, so Meta would still
        accept it for seven days — while the contact has demonstrably written to
        the account, so an ordinary DM reaches them.
        Before the fix the next message to that person, an agent's inbox reply
        included, was silently sent as the comment's one private reply: it
        carried Meta's auto-appended link to the post and spent an allowance the
        agent knew nothing about.

        The row is built through ``guards.record_comment`` rather than through a
        webhook because that combination is not reachable from one: L4-A's
        routing takes the claim only when the commenter has no contact yet.
        """
        from apps.flows.triggers import guards
        from apps.messaging.models import Message, MessageDirection, MessageSource, MessageStatus

        with fake_graph():
            deliver(client, at_now(load_delivery("message_text")))
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        Message.objects.create(
            conversation=identity.contact.conversations.get(),
            direction=MessageDirection.OUT,
            source=MessageSource.AUTOMATION,
            status=MessageStatus.SENT,
            body={"blocks": [{"type": "text", "text": "earlier"}]},
        )

        row = guards.record_comment(
            connection=instagram_connection,
            trigger=comment_trigger,
            comment_id=COMMENT_ID,
            post_id=POST_ID,
            commenter_ref=IG_USER_ID,
            commented_at=timezone.now(),
        )
        assert row is not None and row.private_reply_sent_at is None

        with fake_graph() as api:
            _send(tenancy, identity.contact, instagram_connection, "Following up", source="agent")

        (body,) = api.message_bodies()
        assert body["recipient"] == {"id": IG_USER_ID}
        row.refresh_from_db()
        assert row.private_reply_sent_at is None

    def test_a_pure_commenter_still_gets_the_private_reply(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """The other side of the same predicate. Somebody who has only ever
        commented has sent no inbound *message*, so Meta will not take an
        ordinary DM and the private reply is the only form that works — even
        though the claim itself set ``last_inbound_at``, which is why that field
        cannot be the signal."""
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
        assert api.message_bodies()[0]["recipient"] == {"comment_id": COMMENT_ID}
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.last_inbound_at is not None


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestPublicReplyIdempotence:
    def test_a_re_run_of_the_handler_posts_no_second_comment(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """``apps.queueing.registry``: "A handler must be safe to run more than
        once" — a worker can die between the handler committing and the row being
        marked, and zombie recovery re-runs it. Without a durable claim that was
        a second visible comment on the customer's post."""
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
            run_queued()
            run_queued()
        assert len(api.bodies(f"{COMMENT_ID}/replies")) == 1

    def test_the_claim_is_taken_even_when_the_reply_is_refused(
        self,
        client: Client,
        tenancy: Tenancy,
        instagram_connection: ChannelConnection,
        comment_trigger: Trigger,
    ) -> None:
        """Taken before the call, not recorded after it: the failure being
        prevented is a duplicate comment on somebody's post, and a claim spent on
        a refused reply costs only that reply."""
        from apps.channels.tests.instagram_support import Reply

        with fake_graph(lambda api: api.reply(f"{COMMENT_ID}/replies", Reply(status=400))) as api:
            deliver(client, at_now(load_delivery("comment")))
            run_queued()
            run_queued()
        assert len(api.bodies(f"{COMMENT_ID}/replies")) == 1
        row = HandledComment.objects.for_workspace(tenancy.workspace).get()
        assert row.public_reply_sent_at is not None


def _send(tenancy: Tenancy, contact: Any, connection: ChannelConnection, text: str, *, source: str) -> Any:
    from apps.channels.events import OutboundMessage, TextBlock
    from apps.messaging import services

    return services.send_outbound(
        workspace=tenancy.workspace,
        contact=contact,
        connection=connection,
        outbound=OutboundMessage(blocks=(TextBlock(text=text),)),
        source=source,
        idempotency_key=f"test:{text}",
    )


class TestTheResponderSeam:
    def test_a_responder_that_raises_does_not_release_the_claim(self, tenancy: Tenancy) -> None:
        """``comments.respond`` swallows, and it has to: the claim is already
        written, and a raise would propagate into ``run_stage``, roll back the
        savepoint it was written in, and hand the guard straight back."""
        calls: list[Any] = []

        def boom(context: Any, trigger: Any, row: Any) -> None:
            calls.append(row)
            raise RuntimeError("provider down")

        comment_responders.register_responder(
            "telegram",
            comment_responders.CommentResponder(respond=boom),
            replace=True,
        )
        try:
            context = type("Ctx", (), {"connection": type("C", (), {"platform": "telegram"})()})()
            comment_responders.respond(context, None, type("Row", (), {"pk": "x"})())
        finally:
            comment_responders.unregister_responder("telegram")
        assert len(calls) == 1

    def test_a_platform_with_no_responder_is_not_an_error(self) -> None:
        context = type("Ctx", (), {"connection": type("C", (), {"platform": "whatsapp"})()})()
        comment_responders.respond(context, None, type("Row", (), {"pk": "x"})())

    def test_a_duplicate_registration_raises(self) -> None:
        """One per platform, and which one wins must not depend on import order."""
        responder = comment_responders.CommentResponder(respond=lambda *a: None)
        with pytest.raises(ValueError, match="already has a comment responder"):
            comment_responders.register_responder("instagram", responder)


def test_the_responder_registry_survives_this_module() -> None:
    """A guard on this module itself. The registry is process-global, and the
    tests above install and remove fakes in it; a leak would leave every later
    test in the process without Instagram's comment automation.

    Membership, not equality: the registry grows by one entry per Layer-5
    platform that ships comment automation (#18 added Messenger), and pinning the
    whole tuple would turn every one of those into a failure here rather than in
    the workstream that changed something."""
    assert "instagram" in comment_responders.registered_platforms()
