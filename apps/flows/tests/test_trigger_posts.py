"""The comment trigger's post picker (SPEC §10, issues #17 and #18).

Two things are being tested and they belong to different layers.

The **seam** is ``apps.channels.posts``: a registry, so a platform gains a picker
by registering a lister rather than by editing this view or its template. A
platform with none registered gets an explanation, not an error.

The **view** is ordinary workspace-scoped CRUD, so it owes what every such route
owes — ``edit_flows``, cross-tenant 404s, and platform text escaped on render
(SECURITY-BASELINE §§1, 2). The automatic sweep in ``tests/test_idor.py`` covers
the route as soon as it exists, because both of its ids already have resolvers;
what is here is what that sweep cannot see.
"""

from typing import Any

import pytest
from django.urls import reverse

from apps.channels.models import ChannelConnection
from apps.channels.posts import Post, PostListingError, register_post_lister
from apps.channels.providers import meta_common
from apps.channels.tests.messenger_support import PAGE_TOKEN, Reply, fake_graph
from apps.common.platforms import Platform
from apps.flows.models import Flow
from apps.flows.tests.support import graph, node, published_flow
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy, create_tenancy

pytestmark = pytest.mark.django_db


@pytest.fixture
def flow(tenancy: Tenancy) -> Flow:
    return published_flow(
        tenancy.workspace,
        graph([node("start", "send_message", {"blocks": [{"type": "text", "text": "hi"}]})]),
        name="Comment flow",
    )


def messenger_page(workspace: Any, *, external_id: str = "111111111111111") -> ChannelConnection:
    connection = ChannelConnection(
        workspace=workspace,
        platform=Platform.MESSENGER.value,
        display_name="Acme Page",
        external_id=external_id,
    )
    meta_common.store_page_token(connection, PAGE_TOKEN)
    connection.save()
    return connection


def picker_url(tenancy: Tenancy, flow: Flow, connection: ChannelConnection) -> str:
    return reverse(
        "flows:trigger_posts",
        kwargs={
            "workspace_id": tenancy.workspace.pk,
            "flow_id": flow.pk,
            "connection_id": connection.pk,
        },
    )


def listing(rows: list[dict[str, Any]]) -> Any:
    def configure(fake: Any) -> None:
        fake.reply("/posts", Reply(body={"data": rows}))

    return configure


POSTS = [
    {
        "id": "111111111111111_8001",
        "message": "New drop, out now",
        "permalink_url": "https://facebook.test/p/8001",
        "created_time": "2026-08-01T09:00:00+0000",
    },
    {"id": "111111111111111_8002", "message": "", "permalink_url": "", "created_time": ""},
]


class TestPermissions:
    def test_an_editor_can_open_the_picker(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        connection = messenger_page(tenancy.workspace)
        with fake_graph(listing(POSTS)):
            response = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).get(picker_url(tenancy, flow, connection))
        assert response.status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_everyone_else_is_refused(self, tenancy: Tenancy, client_for: Any, flow: Flow, role: str) -> None:
        connection = messenger_page(tenancy.workspace)
        response = client_for(tenancy.user_for(role)).get(picker_url(tenancy, flow, connection))
        assert response.status_code == 403

    def test_another_tenants_connection_is_a_404(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        """Their id, our workspace — the case ``get_scoped_object_or_404`` catches.

        404 rather than 403: a 403 would confirm the id names something real,
        which over a UUID space is the only thing an attacker was missing.
        """
        rival = create_tenancy("rival")
        theirs = messenger_page(rival.workspace, external_id="222222222222222")
        response = client_for(tenancy.owner).get(picker_url(tenancy, flow, theirs))
        assert response.status_code == 404

    def test_another_tenants_flow_is_a_404(self, tenancy: Tenancy, client_for: Any) -> None:
        rival = create_tenancy("rival")
        theirs = published_flow(
            rival.workspace,
            graph([node("start", "send_message", {"blocks": [{"type": "text", "text": "hi"}]})]),
            name="Theirs",
        )
        connection = messenger_page(tenancy.workspace)
        response = client_for(tenancy.owner).get(picker_url(tenancy, theirs, connection))
        assert response.status_code == 404


class TestListing:
    def test_it_lists_the_pages_posts(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        connection = messenger_page(tenancy.workspace)
        with fake_graph(listing(POSTS)) as fake:
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        body = response.content.decode()
        assert "New drop, out now" in body
        assert "111111111111111_8001" in body
        # The id is what a trigger stores, so it is the checkbox's value.
        assert 'value="111111111111111_8001"' in body
        assert fake.calls[0].params["fields"] == "id,message,permalink_url,created_time"

    def test_a_post_with_no_text_still_appears(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        connection = messenger_page(tenancy.workspace)
        with fake_graph(listing(POSTS)):
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        assert b"No text" in response.content

    def test_an_account_with_no_posts_is_explained_rather_than_failed(
        self, tenancy: Tenancy, client_for: Any, flow: Flow
    ) -> None:
        connection = messenger_page(tenancy.workspace)
        with fake_graph(listing([])):
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        assert response.status_code == 200
        assert b"has not posted anything yet" in response.content

    def test_a_refused_listing_answers_200_with_a_reason(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        """htmx drops ``HX-Trigger`` on a non-2xx, so a 4xx would do nothing visible."""
        connection = messenger_page(tenancy.workspace)

        def configure(fake: Any) -> None:
            fake.reply("/posts", Reply(status=400))

        with fake_graph(configure):
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        assert response.status_code == 200
        assert b"may need reconnecting" in response.content

    def test_a_platform_with_no_lister_says_so(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        """Telegram has no comments and therefore no picker."""
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM.value,
            display_name="A bot",
            external_id="bot-1",
        )
        response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        assert response.status_code == 200
        assert b"cannot list posts" in response.content

    def test_post_text_is_escaped(self, tenancy: Tenancy, client_for: Any, flow: Flow) -> None:
        """A page's own text is written by whoever runs the page."""
        connection = messenger_page(tenancy.workspace)
        hostile = [{"id": "1_2", "message": "<script>alert(1)</script>", "permalink_url": "", "created_time": ""}]
        with fake_graph(listing(hostile)):
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        body = response.content.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


class TestTheSeam:
    def test_messenger_registers_its_lister_on_import(self) -> None:
        from apps.channels import posts as channel_posts
        from apps.channels.providers import messenger

        assert channel_posts.post_lister_for(Platform.MESSENGER.value) is messenger.list_posts

    def test_a_platform_without_one_is_not_offered_a_picker(self) -> None:
        """L5-A adds one ``register_post_lister`` line, not an edit to the view."""
        from apps.channels import posts as channel_posts

        assert channel_posts.supports_post_listing(Platform.MESSENGER.value) is True
        assert channel_posts.supports_post_listing(Platform.TELEGRAM.value) is False

    def test_a_duplicate_registration_raises(self) -> None:
        """Two listers for one platform is a merge accident, like two adapters."""
        with pytest.raises(ValueError, match="already has a post lister"):
            register_post_lister(Platform.MESSENGER.value, lambda connection, limit: [])

    def test_the_requested_limit_is_clamped(self, tenancy: Tenancy) -> None:
        """It reaches a Graph query string, so it is bounded rather than trusted."""
        from apps.channels import posts as channel_posts

        seen: list[int] = []

        def record(connection: Any, limit: int) -> list[Post]:
            seen.append(limit)
            return []

        register_post_lister("telegram", record, replace=True)
        try:
            connection = ChannelConnection.objects.create(
                workspace=tenancy.workspace,
                platform=Platform.TELEGRAM.value,
                display_name="A bot",
                external_id="bot-limit",
            )
            channel_posts.list_posts(connection, limit=10_000)
            channel_posts.list_posts(connection, limit=0)
        finally:
            channel_posts._LISTERS.pop("telegram", None)
        assert seen == [channel_posts.MAX_POST_LIMIT, 1]

    def test_an_unregistered_platform_raises_rather_than_answering_empty(self, tenancy: Tenancy) -> None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.WHATSAPP.value,
            display_name="A number",
            external_id="wa-1",
        )
        with pytest.raises(PostListingError):
            from apps.channels import posts as channel_posts

            channel_posts.list_posts(connection)

    def test_clean_post_drops_a_row_with_no_id(self) -> None:
        from apps.channels.posts import clean_post

        assert clean_post(post_id="", title="x", permalink="", created_time="") is None
        assert clean_post(post_id=None, title="x", permalink="", created_time="") is None
        assert isinstance(clean_post(post_id="1_2", title=42, permalink=[], created_time=None), Post)

    @pytest.mark.parametrize(
        "permalink",
        ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "vbscript:x", "//evil.test/x"],
    )
    def test_a_permalink_that_would_execute_is_dropped(self, permalink: str) -> None:
        """Escaping does not help here: the scheme is the payload.

        Django escaping ``javascript:alert(1)`` leaves a link that still runs when
        somebody clicks *View*, so the check has to be on the scheme
        (``apps.common.validators.is_renderable_url``, SECURITY-BASELINE §2). The
        post still appears in the picker — it just has no link.
        """
        from apps.channels.posts import clean_post

        post = clean_post(post_id="1_2", title="A post", permalink=permalink, created_time="")
        assert post is not None
        assert post.permalink == ""
        assert post.id == "1_2"

    def test_an_ordinary_permalink_survives(self) -> None:
        from apps.channels.posts import clean_post

        post = clean_post(post_id="1_2", title="A post", permalink="https://facebook.test/p/1", created_time="")
        assert post is not None
        assert post.permalink == "https://facebook.test/p/1"

    def test_a_hostile_permalink_never_reaches_the_rendered_page(
        self, tenancy: Tenancy, client_for: Any, flow: Flow
    ) -> None:
        connection = messenger_page(tenancy.workspace)
        hostile = [
            {"id": "1_2", "message": "Click me", "permalink_url": "javascript:alert(1)", "created_time": ""},
        ]
        with fake_graph(listing(hostile)):
            response = client_for(tenancy.owner).get(picker_url(tenancy, flow, connection))
        body = response.content.decode()
        assert "javascript:" not in body
        assert "Click me" in body
