"""Story mentions, story replies, follows — and the comment trigger's post picker.

The three trigger types SPEC §21 puts in phase 3 and SPEC §10 lists as
Instagram-only. Their *matchers* are platform-agnostic and tested in
``apps/flows/tests/test_trigger_matching.py``; what is tested here is the half
that is genuinely this adapter's — that a real delivery turns into the right
event type and reaches the right trigger through the production pipeline.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection
from apps.channels.providers.meta_common import SIGNATURE_HEADER
from apps.channels.tests.instagram_support import Reply, at_now, fake_graph, load_delivery, sign
from apps.flows.models import Flow, FlowExecution, Trigger, TriggerType
from apps.flows.tests.support import graph as flow_graph
from apps.flows.tests.support import node
from apps.messaging.models import ContactChannelIdentity
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/instagram/"

REPLY_FLOW = flow_graph([node("say", "send_message", {"blocks": [{"type": "text", "text": "Thanks!"}]})], [])


@pytest.fixture
def real_pipeline() -> Iterator[None]:
    from apps.flows.triggers.pipeline import register_routing
    from apps.messaging.ingest import register_processors

    register_processors()
    register_routing()
    yield


@pytest.fixture
def thanks_flow(tenancy: Tenancy) -> Flow:
    from apps.flows.services import create_flow, publish, save_draft

    flow = create_flow(workspace=tenancy.workspace, name="Thanks")
    save_draft(flow, REPLY_FLOW)
    publish(flow)
    flow.refresh_from_db()
    return flow


def trigger_for(flow: Flow, connection: ChannelConnection, kind: str, config: Any = None) -> Trigger:
    trigger = Trigger(flow=flow, channel_connection=connection, type=kind, config_json=config or {})
    trigger.save()
    return trigger


def deliver(client: Client, payload: dict[str, Any]) -> Any:
    body = json.dumps(payload).encode()
    return client.post(WEBHOOK_URL, data=body, content_type="application/json", headers={SIGNATURE_HEADER: sign(body)})


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestStoryTriggers:
    def test_a_story_mention_starts_its_flow(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, thanks_flow: Flow
    ) -> None:
        trigger_for(thanks_flow, instagram_connection, TriggerType.STORY_MENTION)
        with fake_graph() as api:
            assert deliver(client, at_now(load_delivery("story_mention"))).status_code == 200
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1
        assert api.messages()[0]["text"] == "Thanks!"

    def test_a_story_mention_opens_the_messaging_window(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, thanks_flow: Flow
    ) -> None:
        """It is the contact opening a conversation, so a reply is permitted —
        which is what lets the flow's first message past SPEC §8's chokepoint."""
        trigger_for(thanks_flow, instagram_connection, TriggerType.STORY_MENTION)
        with fake_graph():
            deliver(client, at_now(load_delivery("story_mention")))
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.window_expires_at is not None

    def test_a_story_reply_matches_its_keywords(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, thanks_flow: Flow
    ) -> None:
        trigger_for(
            thanks_flow,
            instagram_connection,
            TriggerType.STORY_REPLY,
            {"keywords": [{"text": "love", "mode": "contains"}]},
        )
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("story_reply")))
        assert api.messages()[0]["text"] == "Thanks!"

    def test_a_story_reply_that_misses_the_keywords_does_nothing(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, thanks_flow: Flow
    ) -> None:
        trigger_for(
            thanks_flow,
            instagram_connection,
            TriggerType.STORY_REPLY,
            {"keywords": [{"text": "refund", "mode": "contains"}]},
        )
        with fake_graph() as api:
            deliver(client, at_now(load_delivery("story_reply")))
        assert api.messages() == []

    def test_a_story_reply_is_a_thread_message(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """Unlike a mention, a reply is something the contact typed at us, so it
        belongs in the thread an agent reads."""
        from apps.messaging.models import Message, MessageDirection

        with fake_graph():
            deliver(client, at_now(load_delivery("story_reply")))
        message = Message.objects.for_workspace(tenancy.workspace).get(direction=MessageDirection.IN)
        assert message.body["blocks"][0]["text"] == "love this"


@pytest.mark.usefixtures("instagram_app", "real_pipeline")
class TestFollowTrigger:
    def test_a_follow_creates_a_contact_but_grants_no_consent(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, thanks_flow: Flow
    ) -> None:
        """SPEC §10's follow trigger, and the reason it is not a message: following
        an account is a relationship, not permission to message back. The
        compliance engine refuses the send, which is correct rather than a bug."""
        trigger_for(thanks_flow, instagram_connection, TriggerType.FOLLOW)
        with fake_graph() as api:
            assert deliver(client, at_now(load_delivery("follow"))).status_code == 200

        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opt_in is False
        assert identity.window_expires_at is None
        assert api.messages() == []

    def test_the_follow_matcher_is_no_longer_a_stub(self) -> None:
        """The field is not published for this product, so the trigger fires only
        if Meta ever grants one — SPEC §10's "degrade gracefully". The parser and
        the matcher are real either way, which is what makes that a
        configuration difference rather than a code change."""
        from apps.flows.triggers.matching import registered_matchers
        from apps.flows.triggers.types import STUB_TYPES
        from apps.flows.triggers.types import TriggerType as Types

        assert Types.FOLLOW in registered_matchers()
        assert Types.FOLLOW not in STUB_TYPES


class TestPostPicker:
    """SPEC §10's comment-trigger config, which until now took typed-in post ids."""

    def url(self, tenancy: Tenancy) -> str:
        return reverse("channels:instagram_posts", kwargs={"workspace_id": tenancy.workspace.pk})

    def test_it_lists_recent_media(
        self, client_for: Any, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        media = {
            "data": [
                {
                    "id": "17800000000000200",
                    "caption": "New kit",
                    "media_type": "IMAGE",
                    "media_url": "https://cdn.test/a.jpg",
                    "permalink": "https://www.instagram.com/p/abc/",
                    "timestamp": "2026-08-01T09:00:00+0000",
                }
            ]
        }
        with fake_graph(lambda api: api.reply("me/media", Reply(body=media))):
            response = client_for(tenancy.owner).get(self.url(tenancy))
        assert response.status_code == 200
        assert b"17800000000000200" in response.content
        assert b"New kit" in response.content

    def test_a_hostile_caption_is_escaped(
        self, client_for: Any, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """A caption is whatever somebody typed into Instagram
        (SECURITY-BASELINE §2). Nothing in the picker marks it safe."""
        media = {"data": [{"id": "1", "caption": "<script>alert(1)</script>", "media_type": "IMAGE"}]}
        with fake_graph(lambda api: api.reply("me/media", Reply(body=media))):
            response = client_for(tenancy.owner).get(self.url(tenancy))
        assert b"<script>alert(1)</script>" not in response.content
        assert b"&lt;script&gt;" in response.content

    def test_no_connection_is_an_explanation_not_an_error(self, client_for: Any, tenancy: Tenancy) -> None:
        """The caller is an htmx fragment; a 4xx would show a failure where
        "connect Instagram first" is an ordinary thing to render."""
        response = client_for(tenancy.owner).get(self.url(tenancy))
        assert response.status_code == 200
        assert b"Connect an Instagram account" in response.content

    def test_a_refused_listing_is_an_explanation_too(
        self, client_for: Any, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        with fake_graph(lambda api: api.reply("me/media", Reply(status=400))):
            response = client_for(tenancy.owner).get(self.url(tenancy))
        assert response.status_code == 200
        assert b"would not list" in response.content

    def test_it_needs_edit_flows(
        self, client_for: Any, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        """``edit_flows`` rather than ``manage_channels``: the person using it is
        a flow author, and it reads the connection without changing it."""
        assert client_for(tenancy.user_for("editor")).get(self.url(tenancy)).status_code == 200
        assert client_for(tenancy.user_for("viewer")).get(self.url(tenancy)).status_code == 403

    def test_another_tenant_gets_404(
        self, client_for: Any, tenancy: Tenancy, other_tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        assert client_for(other_tenancy.owner).get(self.url(tenancy)).status_code == 404


class TestTheTriggerForm:
    """The two things the form reads off the comment-responder registry."""

    def form_url(self, tenancy: Tenancy, flow: Flow) -> str:
        return reverse(
            "flows:trigger_form",
            kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk},
        )

    def test_the_like_checkbox_is_absent_for_instagram(
        self, client_for: Any, tenancy: Tenancy, thanks_flow: Flow
    ) -> None:
        """Meta publishes no way to like an Instagram comment, so offering the
        option would be offering something that silently never happens."""
        response = client_for(tenancy.owner).get(self.form_url(tenancy, thanks_flow), {"type": "comment"})
        assert response.status_code == 200
        assert b'name="like_comment"' not in response.content

    def test_the_post_picker_is_offered(self, client_for: Any, tenancy: Tenancy, thanks_flow: Flow) -> None:
        response = client_for(tenancy.owner).get(self.form_url(tenancy, thanks_flow), {"type": "comment"})
        assert b"Choose from Instagram" in response.content
        assert (
            reverse("channels:instagram_posts", kwargs={"workspace_id": tenancy.workspace.pk}).encode()
            in response.content
        )

    def test_a_stored_like_setting_survives_an_edit(
        self, client_for: Any, tenancy: Tenancy, thanks_flow: Flow, instagram_connection: ChannelConnection
    ) -> None:
        """Hidden, not dropped. An unchecked checkbox posts nothing, so omitting
        the control would silently clear a setting the author made."""
        trigger = trigger_for(
            thanks_flow,
            instagram_connection,
            TriggerType.COMMENT,
            {"post_scope": "all", "like_comment": True, "public_reply": {"mode": "none", "texts": []}},
        )
        response = client_for(tenancy.owner).get(self.form_url(tenancy, thanks_flow), {"trigger": str(trigger.pk)})
        assert b'type="hidden" name="like_comment"' in response.content
