"""The inbox is the primary attacker-content → team-browser path (baseline §2).

Everything a thread renders — message text, captions, card titles, button
labels, contact names, platform user ids, attachment URLs — arrived over a
webhook from a stranger. This suite is the issue's "dedicated hostile-content
test suite" and it asserts three separate things, because they fail
independently:

1. Hostile text is stored exactly as delivered and rendered inert.
2. A URL that is not http(s) never becomes an ``href`` or a ``src``.
3. The app has no way to bypass autoescaping in the first place — an AST scan,
   so a future edit cannot reintroduce one without turning this red.

The corpus is ``apps/messaging/tests/hostile.py``, whose own docstring says it
was exported "so L4-D reuses it rather than growing a second, differently-wrong
copy of the list".
"""

import ast
import pathlib
import re
from typing import Any

import pytest
from django.utils.html import escape

from apps.inbox.rendering import Card, Image, Link, Text, Tombstone, render_message
from apps.messaging.models import Conversation, Message, MessageDirection, MessageStatus
from apps.messaging.tests.hostile import INJECTIONS, OVERSIZED, WRONG_TYPES

pytestmark = pytest.mark.django_db

#: Schemes that execute or impersonate if a browser is allowed to follow them.
DANGEROUS_URLS = (
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//evil.test/steal",
    "javascript\t:alert(1)",
)


def _thread(client: Any, url_for: Any, conversation: Conversation) -> str:
    return client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()


def assert_escaped(payload: str, body: str) -> None:
    """``payload`` is present, escaped, and never as live markup.

    Asserting on substrings like ``"onerror="`` does not work and is worth
    saying why: escaping ``<img src=x onerror=alert(1)>`` yields
    ``&lt;img src=x onerror=alert(1)&gt;``, which still *contains* "onerror=" as
    ordinary text while being completely inert. The property that actually
    matters is that the angle brackets were escaped, so the comparison is
    against Django's own escape() rather than against a list of scary words.
    """
    rendered = escape(payload)
    assert rendered in body, f"{payload!r} did not render at all"
    if rendered != payload:
        assert payload not in body, f"{payload!r} survived unescaped"


class TestHostileText:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_it_is_stored_verbatim_and_rendered_inert(
        self, payload: str, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        message = inbound(payload)
        message.refresh_from_db()

        body = _thread(agent_client, url_for, conversation)

        # Stored as delivered — the inbox is a record of what was said, and
        # sanitising on write would make it a record of something else.
        assert message.body["blocks"][0]["text"] == payload
        # ...and inert on the way out.
        assert_escaped(payload, body)

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_a_hostile_contact_name_is_inert_in_the_list_and_the_panel(
        self, payload: str, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """A display name is a platform profile field, so it is attacker-authored
        exactly like message text — and it renders in three places."""
        conversation.contact.first_name = payload[:150]
        conversation.contact.last_name = ""
        conversation.contact.save(update_fields=["first_name", "last_name", "updated_at"])
        # What the page actually renders. Contact.display_name joins and strips,
        # so comparing against the raw field would fail on a payload that opens
        # with whitespace — for the wrong reason.
        shown = conversation.contact.display_name

        page = agent_client.get(
            url_for("thread", conversation_id=conversation.pk), headers={"HX-Request": "true"}
        ).content.decode()

        assert_escaped(shown, page)

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_a_hostile_platform_user_id_is_inert_in_the_sidebar(
        self, payload: str, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        handle = payload[:200]
        identity.platform_user_id = handle
        identity.save(update_fields=["platform_user_id", "updated_at"])

        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert_escaped(handle, panel)

    #: A payload whose evaluation would be *visible*, which the obvious choice is
    #: not. ``{{ 7*7 }}`` is the idiom everyone reaches for and it proves nothing
    #: here: Django does no arithmetic, so that expression does not render as 49
    #: — it raises ``TemplateSyntaxError`` at compile time ("Could not parse the
    #: remainder: '*7'"). Asserting ``"49" not in body`` therefore could not fail
    #: for the reason it named, and could only fail for one it did not: the
    #: fragment carries UUID ids in its URLs, roughly one in twenty of which
    #: contains "49" — and message timestamps, so any run at 16:49 failed too.
    #: #20 found the second collision independently while #19 found the first,
    #: which is the argument against a short numeric needle rather than against
    #: either particular source of digits.
    #:
    #: These two would both change under evaluation, unmistakably: the filter
    #: upper-cases its argument, and the ``{% %}`` tag is consumed rather than
    #: printed. Neither result can collide with a lowercase-hex id.
    SSTI_PROBE = '{{ "injected"|upper }} and {% load static %}'

    def test_template_syntax_in_a_message_is_never_evaluated(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """SECURITY-BASELINE §3's SSTI ban, from the reader's side: a contact who
        types a template tag sees a template tag."""
        inbound(self.SSTI_PROBE)

        body = _thread(agent_client, url_for, conversation)

        # Present, escaped, and not present raw — the same property every other
        # hostile payload in this file is held to.
        assert_escaped(self.SSTI_PROBE, body)
        # The tag survived rather than being parsed away.
        assert "{% load static %}" in body
        # And the filter never ran. Uppercase, so it cannot match a timestamp or
        # an id.
        assert "INJECTED" not in body

    @pytest.mark.parametrize("payload", OVERSIZED)
    def test_an_oversized_message_renders_without_blowing_up(
        self, payload: str, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        inbound(payload)

        assert "<script" not in _thread(agent_client, url_for, conversation)

    def test_an_oversized_message_is_truncated_in_the_list_preview(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """A hundred rows each carrying a hundred-thousand-character message is
        a ten-megabyte response every three seconds."""
        inbound("A" * 200_000)

        rows = agent_client.get(url_for("rows")).content.decode()

        assert len(rows) < 20_000


class TestHostileUrls:
    @pytest.mark.parametrize("url", DANGEROUS_URLS)
    def test_a_dangerous_attachment_url_becomes_a_tombstone(
        self, url: str, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """apps.messaging.ingest stores attachment URLs verbatim — it caps
        length and strips NULs and deliberately does not look at the scheme —
        so one of these really can be sitting in a body."""
        inbound(blocks=[{"type": "image", "url": url, "caption": ""}])

        body = _thread(agent_client, url_for, conversation)

        assert url not in body
        assert "javascript:" not in body.lower()
        assert "ib-tombstone" in body

    @pytest.mark.parametrize("url", DANGEROUS_URLS)
    def test_it_is_refused_in_a_card_and_in_a_button_too(self, url: str, conversation: Conversation) -> None:
        """Same discipline on every URL-bearing field, not only the obvious one."""
        message = Message(
            conversation=conversation,
            direction=MessageDirection.IN,
            status=MessageStatus.DELIVERED,
            body={
                "blocks": [
                    {
                        "type": "card",
                        "title": "t",
                        "image_url": url,
                        "url": url,
                        "buttons": [{"id": "b", "label": "Press", "url": url}],
                    }
                ]
            },
        )

        card = render_message(message).parts[0]

        assert isinstance(card, Card)
        assert card.image_url == ""
        assert card.url == ""
        assert card.buttons[0].url == ""

    def test_a_gallery_card_renders_everything_a_standalone_card_does(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The gallery branch was a copy of the card branch that had lost its
        subtitle and its buttons, so a carousel arrived in the thread with the
        text and the actions missing. One shared partial now serves both."""
        card = {
            "title": "Blue jacket",
            "subtitle": "In stock, ships tomorrow",
            "image_url": "https://cdn.example.test/j.png",
            "url": "https://shop.example.test/j",
            "buttons": [{"id": "b1", "label": "Buy it", "url": "https://shop.example.test/buy"}],
        }
        inbound(blocks=[{"type": "gallery", "cards": [card]}])

        body = _thread(agent_client, url_for, conversation)

        assert "Blue jacket" in body
        assert "In stock, ships tomorrow" in body
        assert "Buy it" in body

    def test_a_gallery_cards_urls_go_through_the_same_scheme_check(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """Sharing the partial must not mean sharing a hole: the vetting happens
        in rendering.py before either branch sees a Card."""
        inbound(
            blocks=[
                {
                    "type": "gallery",
                    "cards": [
                        {
                            "title": "t",
                            "image_url": "javascript:alert(1)",
                            "url": "javascript:alert(1)",
                            "buttons": [{"id": "b", "label": "Press", "url": "javascript:alert(1)"}],
                        }
                    ],
                }
            ]
        )

        body = _thread(agent_client, url_for, conversation)

        assert "javascript:" not in body.lower()

    def test_an_ordinary_https_attachment_still_renders(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The check has to let real attachments through, or it is just a
        content filter with extra steps."""
        inbound(blocks=[{"type": "image", "url": "https://cdn.example.test/a.png", "caption": "look"}])

        body = _thread(agent_client, url_for, conversation)

        assert "https://cdn.example.test/a.png" in body
        assert "ib-tombstone" not in body

    def test_non_image_media_is_a_link_rather_than_an_inline_player(self, conversation: Conversation) -> None:
        """Not a style choice. The CSP's media-src is 'self' blob:, so an
        <audio>/<video> pointed at a platform CDN is blocked by the browser and
        renders as a broken control; img-src allows https:, so an image is fine.
        """
        message = Message(
            conversation=conversation,
            direction=MessageDirection.IN,
            body={
                "blocks": [
                    {"type": "image", "url": "https://cdn.example.test/a.png"},
                    {"type": "video", "url": "https://cdn.example.test/a.mp4"},
                    {"type": "audio", "url": "https://cdn.example.test/a.mp3"},
                    {"type": "file", "url": "https://cdn.example.test/a.pdf"},
                ]
            },
        )

        parts = render_message(message).parts

        assert isinstance(parts[0], Image)
        assert all(isinstance(part, Link) for part in parts[1:])

    def test_every_outbound_link_is_opener_safe(
        self, agent_client: Any, url_for: Any, conversation: Conversation, inbound: Any
    ) -> None:
        """The destination is chosen by the same stranger who wrote the message."""
        inbound(blocks=[{"type": "file", "url": "https://cdn.example.test/a.pdf", "caption": "invoice"}])

        body = _thread(agent_client, url_for, conversation)

        for anchor in re.findall(r"<a\b[^>]*https://cdn\.example\.test[^>]*>", body):
            assert 'rel="noopener noreferrer"' in anchor


class TestMalformedBodies:
    @pytest.mark.parametrize("value", WRONG_TYPES)
    def test_a_body_of_the_wrong_type_renders_a_tombstone(self, value: Any, conversation: Conversation) -> None:
        """An adapter should never emit these, and "should never" is exactly the
        assumption a rendering layer does not get to make."""
        rendered = render_message(Message(conversation=conversation, direction=MessageDirection.IN, body=value))

        assert rendered.parts
        assert all(isinstance(part, Tombstone) for part in rendered.parts)

    def test_an_unrecognised_block_type_is_accounted_for_rather_than_dropped(self, conversation: Conversation) -> None:
        """apps.messaging.rendering drops what it does not recognise, which is
        right for a retry — sending less beats sending something wrong — and
        wrong for a reader, who would be looking at a hole with nothing to say
        one is there."""
        message = Message(
            conversation=conversation,
            direction=MessageDirection.IN,
            body={"blocks": [{"type": "text", "text": "hi"}, {"type": "hologram", "url": "x"}]},
        )

        parts = render_message(message).parts

        assert isinstance(parts[0], Text)
        assert isinstance(parts[1], Tombstone)

    def test_an_empty_body_is_a_tombstone(self, conversation: Conversation) -> None:
        rendered = render_message(Message(conversation=conversation, direction=MessageDirection.IN, body={}))

        assert isinstance(rendered.parts[0], Tombstone)

    def test_a_tombstone_never_quotes_the_value_it_refused(self, conversation: Conversation) -> None:
        """The reason line is copy. Echoing the rejected payload back into the
        page would hand the browser the exact string we just declined to."""
        message = Message(
            conversation=conversation,
            direction=MessageDirection.IN,
            body={"blocks": [{"type": "image", "url": "javascript:alert(1)"}]},
        )

        tombstone = render_message(message).parts[0]

        assert isinstance(tombstone, Tombstone)
        assert "javascript" not in tombstone.reason


class TestTheAppCannotBypassEscaping:
    def test_no_module_reaches_for_mark_safe(self) -> None:
        """The same AST scan apps.messaging makes about itself. Escaping is the
        whole defence for text here, so the ability to switch it off is the one
        thing this package must not have."""
        banned = {"mark_safe", "format_html", "SafeString"}
        package = pathlib.Path(__file__).resolve().parent.parent
        offenders: list[str] = []

        for path in package.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned:
                    offenders.append(f"{path.name}:{node.lineno} {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in banned:
                    offenders.append(f"{path.name}:{node.lineno} {node.attr}")

        assert offenders == []

    def test_no_inbox_template_disables_autoescaping(self) -> None:
        """``|safe`` and ``{% autoescape off %}`` are the template-side versions
        of the same switch."""
        templates = pathlib.Path(__file__).resolve().parents[3] / "templates" / "inbox"
        offenders = [
            path.name
            for path in templates.glob("*.html")
            if "|safe" in path.read_text() or "autoescape off" in path.read_text()
        ]

        assert offenders == []


class TestContentSecurityPolicy:
    def test_every_inline_script_on_the_inbox_carries_a_nonce(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """script-src has no 'unsafe-inline', so a nonce-less inline script is a
        block in production and silence in development's report-only mode."""
        page = agent_client.get(url_for("list")).content.decode()

        for tag in re.findall(r"<script\b[^>]*>", page):
            assert "src=" in tag or "nonce=" in tag, tag

    def test_the_page_has_no_inline_event_handler_attributes(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """The failure mode is a button that looks fine and is inert."""
        page = agent_client.get(url_for("thread", conversation_id=conversation.pk)).content.decode()

        assert not re.search(r"\son(click|change|submit|load|error)\s*=", page)
