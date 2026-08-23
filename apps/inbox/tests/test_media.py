"""Inbound media in a thread: the block, the tag, and the route behind it.

The bug this closes: an agent reading a thread saw the text of a picture
message and no picture. ``apps.messaging.ingest`` wrote nothing for
``payload.media_ids`` and this app rendered nothing for them.

What the route must be is as much of the point as that it works. It is
authenticated and workspace-scoped rather than a signed public URL, and the
identifier is read out of the stored row rather than taken from the request —
so the tests below check both halves: that a member sees the picture, and that
nobody can steer the credentialed fetch behind it.
"""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from django.urls import reverse

from apps.channels.media import MediaSource
from apps.channels.tests.fake_adapter import FakeAdapter, swapped_adapter
from apps.common.platforms import Platform
from apps.contacts.models import Contact
from apps.inbox.rendering import Link, Media, Tombstone, preview_of, render_message
from apps.messaging.services import open_conversation
from tests.ssrf import FakeInternet, deployment_cache_cleared, serving

pytestmark = pytest.mark.django_db

MEDIA_HOST = "media.example.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/MM1/Media/ME1"


@pytest.fixture(autouse=True)
def _clear_deployment_cache() -> Iterator[None]:
    with deployment_cache_cleared():
        yield


class MediaAdapter(FakeAdapter):
    platform = Platform.TELEGRAM
    asked: list[str] = []

    def media_source(self, connection: Any, media_id: str) -> MediaSource | None:
        type(self).asked.append(media_id)
        return MediaSource(url=f"https://{MEDIA_HOST}/{media_id}")


@pytest.fixture
def media_adapter() -> Iterator[type[MediaAdapter]]:
    adapter_cls = type("MediaAdapterTelegram", (MediaAdapter,), {"asked": []})
    with swapped_adapter(Platform.TELEGRAM, adapter_cls):
        yield adapter_cls


@pytest.fixture
def internet(monkeypatch: Any) -> FakeInternet:
    """The socket and DNS, replaced. The SSRF guard itself stays real."""
    return FakeInternet(serving(PNG), {MEDIA_HOST: [FakeInternet.PUBLIC]}).install(monkeypatch)


@pytest.fixture
def picture(inbound: Callable[..., Any]) -> Any:
    """An inbound message shaped the way ``messaging.ingest`` writes one."""
    return inbound(blocks=[{"type": "text", "text": "look"}, {"type": "media", "media_id": "AgAC", "caption": ""}])


def media_url(message: Any, index: int = 1) -> str:
    return reverse(
        "inbox:media",
        kwargs={
            "workspace_id": message.workspace_id,
            "conversation_id": message.conversation_id,
            "message_id": message.pk,
            "index": index,
        },
    )


class TestRendering:
    def test_a_media_block_becomes_a_part_pointing_at_our_own_route(self, picture: Any) -> None:
        parts = render_message(picture).parts

        assert isinstance(parts[1], Media)
        assert parts[1].url == media_url(picture)

    def test_the_url_addresses_the_block_by_position(self, inbound: Callable[..., Any]) -> None:
        """Position is what lets the view read the id back out of the row."""
        message = inbound(
            blocks=[
                {"type": "media", "media_id": "first", "caption": ""},
                {"type": "text", "text": "between"},
                {"type": "media", "media_id": "second", "caption": ""},
            ]
        )
        parts = render_message(message).parts

        assert [part for part in parts if isinstance(part, Media)] == [
            Media(url=media_url(message, 0)),
            Media(url=media_url(message, 2)),
        ]

    def test_a_block_with_no_identifier_is_a_tombstone(self, inbound: Callable[..., Any]) -> None:
        message = inbound(blocks=[{"type": "media", "caption": ""}])
        assert isinstance(render_message(message).parts[0], Tombstone)

    def test_the_list_preview_uses_the_declared_kind(self, inbound: Callable[..., Any]) -> None:
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": "audio", "caption": ""}])
        assert preview_of(message) == "[audio]"

    def test_the_list_preview_does_not_guess_when_nothing_said(self, inbound: Callable[..., Any]) -> None:
        """An adapter that does not fill ``media_kinds`` gets the honest label
        rather than "[image]"."""
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "caption": ""}])
        assert preview_of(message) == "[attachment]"

    def test_a_preview_never_echoes_an_invented_kind(self, inbound: Callable[..., Any]) -> None:
        """The value arrived over a webhook and reaches the reader's screen."""
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": "<script>", "caption": ""}])
        assert preview_of(message) == "[attachment]"

    def test_the_identifier_never_reaches_the_page(self, agent_client: Any, url_for: Any, inbound: Any) -> None:
        """Twilio's identifier *is* a URL, and handing it to a browser is the
        leak ``media_ids`` exists to avoid — on an account without
        authenticated media it resolves for anyone at all."""
        message = inbound(blocks=[{"type": "media", "media_id": TWILIO_URL, "caption": ""}])

        html = agent_client.get(url_for("thread", conversation_id=message.conversation_id)).content.decode()

        assert TWILIO_URL not in html
        assert media_url(message, 0) in html

    def test_the_thread_renders_an_img(self, agent_client: Any, url_for: Any, picture: Any) -> None:
        html = agent_client.get(url_for("thread", conversation_id=picture.conversation_id)).content.decode()

        assert f'src="{media_url(picture)}"' in html


class TestTheRoute:
    def test_it_serves_the_bytes(self, agent_client: Any, picture: Any, media_adapter: Any, internet: Any) -> None:
        response = agent_client.get(media_url(picture))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"".join(response.streaming_content if response.streaming else [response.content]) == PNG

    def test_the_identifier_comes_from_the_row(self, agent_client: Any, picture: Any, media_adapter: Any, internet):
        """Not from the URL, which names only a message and a position — so a
        request cannot ask this deployment's credentials for an id of its own
        choosing."""
        agent_client.get(media_url(picture))

        assert media_adapter.asked == ["AgAC"]

    def test_a_viewer_may_read_it(self, viewer_client: Any, picture: Any, media_adapter: Any, internet: Any) -> None:
        """SPEC §4.2's Viewer holds ``use_inbox``; a picture is part of reading."""
        assert viewer_client.get(media_url(picture)).status_code == 200

    def test_an_index_past_the_end_is_a_404(self, agent_client: Any, picture: Any, media_adapter: Any) -> None:
        assert agent_client.get(media_url(picture, 9)).status_code == 404

    def test_a_block_that_is_not_media_is_a_404(self, agent_client: Any, picture: Any, media_adapter: Any) -> None:
        """Block 0 of this message is the text."""
        assert agent_client.get(media_url(picture, 0)).status_code == 404

    def test_a_message_in_another_conversation_is_a_404(
        self, agent_client: Any, picture: Any, tenancy: Any, contact: Any, connection: Any, media_adapter: Any
    ) -> None:
        """The message is scoped to the conversation as well as the workspace,
        so a real id from the wrong thread is still a miss."""
        other = open_conversation(
            workspace=tenancy.workspace,
            contact=Contact.objects.create(workspace=tenancy.workspace, first_name="Grace"),
            connection=connection,
        )
        url = reverse(
            "inbox:media",
            kwargs={
                "workspace_id": picture.workspace_id,
                "conversation_id": other.pk,
                "message_id": picture.pk,
                "index": 1,
            },
        )

        assert agent_client.get(url).status_code == 404

    def test_a_platform_that_refuses_is_a_404_and_not_a_500(
        self, agent_client: Any, picture: Any, media_adapter: Any, monkeypatch: Any
    ) -> None:
        FakeInternet(serving(b"no", status=401), {MEDIA_HOST: [FakeInternet.PUBLIC]}).install(monkeypatch)

        assert agent_client.get(media_url(picture)).status_code == 404

    def test_an_adapter_that_raises_is_a_404_and_not_a_500(
        self, agent_client: Any, picture: Any, media_adapter: Any, internet: Any, monkeypatch: Any
    ) -> None:
        """The endpoint's contract is a bare 404 whatever went wrong, and an
        adapter is a plugin point that can break its own promises."""

        def _raise(self: Any, _connection: Any, _media_id: str) -> MediaSource | None:
            raise RuntimeError("an adapter having a bad day")

        monkeypatch.setattr(media_adapter, "media_source", _raise)

        assert agent_client.get(media_url(picture)).status_code == 404

    def test_an_anonymous_request_never_reaches_the_platform(
        self, client: Any, picture: Any, media_adapter: Any, internet: Any
    ) -> None:
        """The session is the credential here; there is no token that stands in
        for one."""
        response = client.get(media_url(picture))

        assert response.status_code in (302, 404)
        assert internet.requests == []
        assert media_adapter.asked == []


class TestTheDeclaredKindPicksTheTag:
    """Rendering the platform's word, without believing it about the bytes.

    Before the kind was carried, every attachment rendered as an ``<img>`` and a
    voice note showed a broken-image icon. The kind decides the *tag*; the
    ``Content-Type`` served back is still sniffed from the bytes, which is the
    half SECURITY-BASELINE §9 governs.
    """

    @pytest.mark.parametrize("kind", ["audio", "video", "file"])
    def test_a_known_non_image_is_a_link(self, inbound: Callable[..., Any], kind: str) -> None:
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": kind, "caption": ""}])

        part = render_message(message).parts[0]

        assert isinstance(part, Link)
        assert part.media_kind == kind
        assert part.url == media_url(message, 0)

    def test_a_declared_image_is_an_img(self, inbound: Callable[..., Any]) -> None:
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": "image", "caption": ""}])
        assert isinstance(render_message(message).parts[0], Media)

    def test_an_undeclared_kind_still_bets_on_an_img(self, inbound: Callable[..., Any]) -> None:
        """An adapter that has not been taught to fill the field keeps working."""
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "caption": ""}])
        assert isinstance(render_message(message).parts[0], Media)

    @pytest.mark.parametrize("kind", ["", "IMAGE", "sticker", "../etc", 42, None, {"a": 1}])
    def test_a_kind_outside_the_vocabulary_is_treated_as_unknown(self, inbound: Callable[..., Any], kind: Any) -> None:
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": kind, "caption": ""}])
        assert isinstance(render_message(message).parts[0], Media)

    def test_a_voice_note_renders_no_broken_image(self, agent_client: Any, url_for: Any, inbound: Any) -> None:
        """The regression this closes, at the level the reader sees it."""
        message = inbound(blocks=[{"type": "media", "media_id": "AgAC", "media_kind": "audio", "caption": ""}])

        html = agent_client.get(url_for("thread", conversation_id=message.conversation_id)).content.decode()

        assert f'src="{media_url(message, 0)}"' not in html
        assert f'href="{media_url(message, 0)}"' in html

    def test_an_images_alt_text_still_says_image(self, agent_client: Any, url_for: Any, inbound: Any) -> None:
        """Merging the two template branches must not demote the Image part's
        accessible name to the vaguer one Media has to use."""
        message = inbound(blocks=[{"type": "image", "url": "https://cdn.example.test/a.jpg", "caption": ""}])

        html = agent_client.get(url_for("thread", conversation_id=message.conversation_id)).content.decode()

        assert 'alt="Attached image"' in html

    def test_a_media_parts_alt_text_promises_less(self, agent_client: Any, url_for: Any, picture: Any) -> None:
        html = agent_client.get(url_for("thread", conversation_id=picture.conversation_id)).content.decode()

        assert 'alt="Attachment"' in html


class TestTheRouteIsConditional:
    """Every fetch is a live platform call on one of four request slots.

    The bytes behind a ``(message, block index)`` never change, so a
    revalidation can be answered without resolving anything — which is what
    stops a thread of pictures re-fetching every one of them on every render.
    """

    def test_a_first_request_carries_an_etag(self, agent_client: Any, picture: Any, media_adapter, internet) -> None:
        response = agent_client.get(media_url(picture))

        assert response.status_code == 200
        assert response["ETag"]
        assert response["Cache-Control"] == "private, max-age=3600"

    def test_a_revalidation_is_a_304(self, agent_client: Any, picture: Any, media_adapter: Any, internet) -> None:
        etag = agent_client.get(media_url(picture))["ETag"]

        response = agent_client.get(media_url(picture), HTTP_IF_NONE_MATCH=etag)

        assert response.status_code == 304

    def test_the_304_never_reaches_the_platform(self, agent_client: Any, picture: Any, media_adapter, internet):
        """The point of the exercise: the saving is the upstream call, not the
        bytes on the wire."""
        etag = agent_client.get(media_url(picture))["ETag"]
        media_adapter.asked.clear()
        internet.requests.clear()

        agent_client.get(media_url(picture), HTTP_IF_NONE_MATCH=etag)

        assert media_adapter.asked == []
        assert internet.requests == []

    def test_the_304_carries_the_caching_headers_too(self, agent_client, picture, media_adapter, internet) -> None:
        """RFC 9110 §15.4.5. Without them the browser drops the entry and
        re-asks on the next render, which is the cost this avoids."""
        etag = agent_client.get(media_url(picture))["ETag"]

        response = agent_client.get(media_url(picture), HTTP_IF_NONE_MATCH=etag)

        assert response["ETag"] == etag
        assert response["Cache-Control"] == "private, max-age=3600"

    def test_a_stale_etag_refetches(self, agent_client: Any, picture: Any, media_adapter: Any, internet) -> None:
        response = agent_client.get(media_url(picture), HTTP_IF_NONE_MATCH='"something-else"')

        assert response.status_code == 200
        assert media_adapter.asked == ["AgAC"]

    def test_two_blocks_on_one_message_do_not_share_a_tag(self, agent_client, inbound, media_adapter, internet):
        """A tag over the row alone would serve the first attachment for the
        second."""
        message = inbound(
            blocks=[
                {"type": "media", "media_id": "first", "caption": ""},
                {"type": "media", "media_id": "second", "caption": ""},
            ]
        )

        first = agent_client.get(media_url(message, 0))["ETag"]
        second = agent_client.get(media_url(message, 1))["ETag"]

        assert first != second
