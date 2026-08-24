"""What :func:`apps.analytics.tracking.instrument` rewrites, and what it leaves alone.

The three promises this file holds to account:

* URL buttons are wrapped on **every** platform (SPEC §18), including the ones
  inside a card or a gallery;
* the two email rewrites happen **only** when the workspace opted in;
* a **preview** run is untouched, which is what keeps a test click from ever
  reaching a counter.
"""

from typing import Any
from urllib.parse import urlsplit

import pytest

from apps.analytics import tracking
from apps.analytics.models import TrackingSettings
from apps.analytics.tests.conftest import ENTRY_NODE, make_execution
from apps.channels.events import Button, Card, CardBlock, GalleryBlock, MediaBlock, OutboundMessage, TextBlock
from apps.channels.providers import email_html
from apps.common.platforms import Platform
from apps.flows.messaging import message_idempotency_key
from apps.messaging.rendering import outbound_from_body

pytestmark = pytest.mark.django_db

TARGET = "https://example.test/pricing"


def wrap(execution: Any, outbound: OutboundMessage, *, platform: str = Platform.TELEGRAM) -> OutboundMessage:
    return tracking.instrument(
        outbound,
        execution=execution,
        node_id=ENTRY_NODE,
        platform=platform,
        idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
    )


def wrapped_target(url: str) -> str:
    """The destination inside a ``/c/`` URL, read back through the signer."""
    token = urlsplit(url).path.split("/")[2]
    return tracking.click_target_from_token(token).url


class TestButtons:
    def test_a_url_button_is_wrapped(self, execution: Any) -> None:
        message = wrap(execution, OutboundMessage(buttons=(Button(id="b1", label="Pricing", url=TARGET),)))

        assert message.buttons[0].url.startswith("http")
        assert "/c/" in message.buttons[0].url
        assert wrapped_target(message.buttons[0].url) == TARGET
        # The label and the id are the flow's, and the id is matched verbatim
        # against a waiting node's handles.
        assert (message.buttons[0].id, message.buttons[0].label) == ("b1", "Pricing")

    def test_a_postback_button_is_untouched(self, execution: Any) -> None:
        message = wrap(execution, OutboundMessage(buttons=(Button(id="yes", label="Yes"),)))

        assert message.buttons[0] == Button(id="yes", label="Yes")

    @pytest.mark.parametrize(
        "platform",
        [Platform.TELEGRAM, Platform.INSTAGRAM, Platform.MESSENGER, Platform.WHATSAPP, Platform.SMS, Platform.EMAIL],
    )
    def test_every_platform_gets_the_wrapper(self, execution: Any, platform: str) -> None:
        """SPEC §18 says "wraps URL buttons […] (all platforms)". Email included:
        it has no button widget, so ``apps.channels.downgrade`` inlines the URL
        into the text — and by then it is already the wrapped one."""
        message = wrap(
            execution,
            OutboundMessage(buttons=(Button(id="b1", label="Pricing", url=TARGET),)),
            platform=str(platform),
        )

        assert "/c/" in message.buttons[0].url

    def test_a_card_s_own_url_and_buttons_are_wrapped(self, execution: Any) -> None:
        card = Card(title="Plan", url=TARGET, buttons=(Button(id="b1", label="Buy", url=TARGET),))
        message = wrap(execution, OutboundMessage(blocks=(CardBlock(card=card),)))

        block = message.blocks[0]
        assert isinstance(block, CardBlock)
        assert wrapped_target(block.card.url) == TARGET
        assert wrapped_target(block.card.buttons[0].url) == TARGET

    def test_every_card_in_a_gallery_is_wrapped(self, execution: Any) -> None:
        cards = tuple(Card(title=f"Card {n}", url=TARGET) for n in range(3))
        message = wrap(execution, OutboundMessage(blocks=(GalleryBlock(cards=cards),)))

        block = message.blocks[0]
        assert isinstance(block, GalleryBlock)
        assert all("/c/" in card.url for card in block.cards)

    def test_a_media_url_is_never_wrapped(self, execution: Any) -> None:
        """A media block's URL is an ``<img src>``, not something a reader clicks;
        pointing it at a redirect would break the image."""
        block = MediaBlock(kind="image", url="https://cdn.test/cat.png")
        message = wrap(execution, OutboundMessage(blocks=(block,)))

        assert message.blocks[0] == block

    def test_a_non_http_button_target_is_left_alone(self, execution: Any) -> None:
        """Nothing in the product produces one; a hand-edited graph can. Leaving
        it unwrapped keeps the refusal in one place — the ``/c/`` view — rather
        than minting a token that route would then 404."""
        message = wrap(execution, OutboundMessage(buttons=(Button(id="b1", label="x", url="mailto:a@b.test"),)))

        assert message.buttons[0].url == "mailto:a@b.test"


class TestPreview:
    def test_a_preview_run_is_returned_untouched(self, flow: Any, contact: Any, connection: Any) -> None:
        execution = make_execution(flow, contact, connection, preview=True)
        outbound = OutboundMessage(buttons=(Button(id="b1", label="Pricing", url=TARGET),))

        assert wrap(execution, outbound) is outbound


class TestEmailBody:
    BODY = '<p>Hello</p><p><a href="https://example.test/a?x=1&amp;y=2">Read more</a></p>'

    def opt_in(self, workspace: Any, **flags: bool) -> None:
        TrackingSettings.objects.update_or_create(workspace=workspace, defaults=flags)

    def test_nothing_is_rewritten_by_default(self, tenancy: Any, execution: Any) -> None:
        """Both toggles are off until a workspace turns them on — a self-hoster
        must not be mailing tracking pixels nobody asked for."""
        outbound = OutboundMessage(html_body=self.BODY, blocks=(TextBlock(text="Hello"),))

        message = wrap(execution, outbound, platform=Platform.EMAIL.value)

        assert message.html_body == self.BODY

    def test_anchors_are_rewritten_when_the_workspace_opts_in(self, tenancy: Any, execution: Any) -> None:
        self.opt_in(tenancy.workspace, wrap_email_links=True)
        outbound = OutboundMessage(html_body=self.BODY)

        message = wrap(execution, outbound, platform=Platform.EMAIL.value)

        assert "/c/" in message.html_body
        assert "https://example.test/a" not in message.html_body

    def test_an_escaped_query_string_survives_the_round_trip(self, tenancy: Any, execution: Any) -> None:
        """The stored href is HTML-escaped, so a naive wrapper would sign
        ``a=1&amp;b=2`` and send the recipient somewhere with a literal
        ``&amp;`` in it."""
        self.opt_in(tenancy.workspace, wrap_email_links=True)

        message = wrap(execution, OutboundMessage(html_body=self.BODY), platform=Platform.EMAIL.value)

        href = message.html_body.split('href="')[1].split('"')[0]
        assert wrapped_target(href) == "https://example.test/a?x=1&y=2"

    def test_a_mailto_anchor_keeps_its_original_markup(self, tenancy: Any, execution: Any) -> None:
        self.opt_in(tenancy.workspace, wrap_email_links=True)
        body = '<p><a href="mailto:sales@example.test">Email us</a></p>'

        message = wrap(execution, OutboundMessage(html_body=body), platform=Platform.EMAIL.value)

        assert message.html_body == body

    def test_the_pixel_is_appended_when_the_workspace_opts_in(self, tenancy: Any, execution: Any) -> None:
        self.opt_in(tenancy.workspace, open_pixel=True)

        message = wrap(execution, OutboundMessage(html_body=self.BODY), platform=Platform.EMAIL.value)

        assert "/o/" in message.html_body
        assert message.html_body.startswith(self.BODY)

    def test_the_pixel_survives_the_adapter_s_sanitiser(self, tenancy: Any, execution: Any) -> None:
        """``email_html.sanitize`` runs on the way out of the email adapter. A
        pixel it stripped would be a counter that never moves."""
        self.opt_in(tenancy.workspace, open_pixel=True)
        message = wrap(execution, OutboundMessage(html_body=self.BODY), platform=Platform.EMAIL.value)

        sanitized = email_html.sanitize(message.html_body)

        assert "/o/" in sanitized
        assert "<img" in sanitized

    def test_a_wrapped_link_survives_the_adapter_s_sanitiser(self, tenancy: Any, execution: Any) -> None:
        self.opt_in(tenancy.workspace, wrap_email_links=True)
        message = wrap(execution, OutboundMessage(html_body=self.BODY), platform=Platform.EMAIL.value)

        assert "/c/" in email_html.sanitize(message.html_body)

    def test_a_non_email_platform_never_touches_the_body(self, tenancy: Any, execution: Any) -> None:
        self.opt_in(tenancy.workspace, wrap_email_links=True, open_pixel=True)

        message = wrap(execution, OutboundMessage(html_body=self.BODY), platform=Platform.TELEGRAM.value)

        assert message.html_body == self.BODY


class TestRetryReproducesTheSameLinks:
    def test_the_wrapped_body_round_trips_through_the_stored_row(self, execution: Any) -> None:
        """A ``send_retry`` rebuilds the message from ``message.body`` hours
        later. Wrapping before the facade persists it is what makes the second
        attempt's buttons carry the same URL as the first — two live keyboards in
        one chat pointing at different links would be worse than no tracking.
        """
        message = wrap(execution, OutboundMessage(buttons=(Button(id="b1", label="Pricing", url=TARGET),)))

        rebuilt = outbound_from_body(message.to_body())

        assert rebuilt.buttons[0].url == message.buttons[0].url
