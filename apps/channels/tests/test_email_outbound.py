"""What an email looks like on the wire, as a table.

``compose`` is pure — no HTTP, no database, no clock beyond the Message-ID — so
almost nothing here needs ``django_db``. That is the same property
``test_telegram_outbound.py`` was written for, and it is what makes the
compliance assertions cheap enough to make on every shape.

The rule this file exists to hold is SPEC §6.7's: **every** email carries
``List-Unsubscribe``, ``List-Unsubscribe-Post`` and a footer link, and nothing
in the config surface can turn any of them off.
"""

from typing import Any, cast

import pytest

from apps.channels.events import Button, Card, CardBlock, GalleryBlock, MediaBlock, OutboundMessage, TextBlock
from apps.channels.providers import email_html
from apps.channels.providers.email import compose

UNSUBSCRIBE = "https://app.test/u/tok123/"


class Identity:
    """The only two things the adapter reads off an identity."""

    def __init__(self, address: str = "reader@example.test") -> None:
        self.platform_user_id = address
        self.opted_out_at = None


class Connection:
    """A stand-in for ChannelConnection: credentials and a workspace id."""

    def __init__(self, **credentials: Any) -> None:
        self.credentials = {
            "provider": "smtp",
            "from_address": "hello@sender.test",
            "from_name": "Sender",
            **credentials,
        }
        self.workspace_id = "ws"
        self.pk = "conn"
        #: SPEC §5's sending domain, which is what a from-override is checked
        #: against.
        self.external_id = "sender.test"


def envelope_for(message: OutboundMessage, **credentials: Any) -> Any:
    # A stand-in rather than a real row, which is what keeps this file free of
    # `django_db`: `compose` reads two attributes and is otherwise pure.
    connection = cast(Any, Connection(**credentials))
    return compose(connection, Identity(), message, unsubscribe_link=UNSUBSCRIBE)


def text_message(body: str, subject: str = "Hello") -> OutboundMessage:
    return OutboundMessage(blocks=(TextBlock(text=body),), subject=subject)


class TestComplianceIsNotOptional:
    """SPEC §6.7: in core, on every email, with no way to switch it off."""

    @pytest.mark.parametrize(
        "message",
        [
            text_message("<p>Hi</p>"),
            OutboundMessage(blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png"),), subject="Pic"),
            OutboundMessage(
                blocks=(CardBlock(card=Card(title="Card", subtitle="Sub")),),
                subject="Card",
            ),
            OutboundMessage(
                blocks=(GalleryBlock(cards=(Card(title="One"), Card(title="Two"))),),
                subject="Gallery",
            ),
            OutboundMessage(
                blocks=(TextBlock(text="<p>Body</p>"),),
                buttons=(Button(id="b", label="Open", url="https://example.test/x"),),
                subject="Buttons",
            ),
        ],
        ids=["text", "image", "card", "gallery", "buttons"],
    )
    def test_every_shape_carries_both_headers(self, message: OutboundMessage) -> None:
        envelope = envelope_for(message)
        assert envelope.headers["List-Unsubscribe"] == f"<{UNSUBSCRIBE}>"
        assert envelope.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_the_footer_link_is_in_both_halves(self) -> None:
        envelope = envelope_for(text_message("<p>Hi</p>"))
        assert UNSUBSCRIBE in envelope.html
        assert UNSUBSCRIBE in envelope.text

    def test_an_empty_body_still_gets_a_footer(self) -> None:
        """A message with nothing in it is refused by send(), not by losing the footer."""
        envelope = envelope_for(OutboundMessage(subject="Empty"))
        assert UNSUBSCRIBE in envelope.html

    def test_the_author_cannot_remove_it_by_writing_their_own(self) -> None:
        """A body containing the word "unsubscribe" does not satisfy the requirement.

        The footer is appended after the author's markup, so a body that already
        mentions unsubscribing gets ours as well rather than instead.
        """
        envelope = envelope_for(text_message("<p>To unsubscribe, reply STOP</p>"))
        assert envelope.html.count(UNSUBSCRIBE) == 1


class TestPlainTextAlternative:
    """SPEC §6.7: "plain-text alternative auto-generated"."""

    def test_it_is_generated_from_the_html(self) -> None:
        envelope = envelope_for(text_message("<p>First</p><p>Second</p>"))
        assert "First" in envelope.text
        assert "Second" in envelope.text
        assert "<p>" not in envelope.text

    def test_links_keep_their_destination(self) -> None:
        html = '<p>See <a href="https://example.test/docs">the docs</a></p>'
        assert "the docs <https://example.test/docs>" in email_html.to_plain_text(html)

    def test_lists_become_bullets(self) -> None:
        text = email_html.to_plain_text("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in text
        assert "- Two" in text

    def test_blank_runs_collapse(self) -> None:
        text = email_html.to_plain_text("<p>A</p><p></p><p></p><p>B</p>")
        assert "\n\n\n" not in text


class TestSanitizer:
    """The allowlist, as a table. Unknown tags unwrap; unknown attributes go."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("<p>Kept</p>", "<p>Kept</p>"),
            ("<script>alert(1)</script>", ""),
            ("<p>Before<script>alert(1)</script>After</p>", "<p>BeforeAfter</p>"),
            ("<style>p{}</style><p>x</p>", "<p>x</p>"),
            ("<section><p>Unwrapped</p></section>", "<p>Unwrapped</p>"),
            ('<p onclick="steal()">x</p>', "<p>x</p>"),
            ('<img src="https://cdn.test/a.png" onerror="x" />', '<img src="https://cdn.test/a.png" />'),
            ('<a href="javascript:alert(1)">x</a>', "<a>x</a>"),
            ('<a href="&#106;avascript:alert(1)">x</a>', "<a>x</a>"),
            ('<a href="mailto:a@b.test">Mail</a>', '<a href="mailto:a@b.test" rel="noopener noreferrer">Mail</a>'),
            ("<!-- [if IE]><script>x</script><![endif] --><p>x</p>", "<p>x</p>"),
            ("<p>a &amp; b</p>", "<p>a &amp; b</p>"),
            ("<p>unclosed", "<p>unclosed</p>"),
            ("</p><p>stray close first</p>", "<p>stray close first</p>"),
            ("<iframe src='https://evil.test'></iframe>", ""),
        ],
    )
    def test_sanitize(self, raw: str, expected: str) -> None:
        assert email_html.sanitize(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # The one that made this a bug rather than a nicety: html.parser
            # reports `&b` as an entityref named "b" even with no semicolon
            # after it, so re-emitting `f"&{name};"` invented a character and
            # turned an ordinary query string into `&b;=2`.
            ("<p>?a=1&b=2</p>", "<p>?a=1&amp;b=2</p>"),
            ("<p>Tom & Jerry</p>", "<p>Tom &amp; Jerry</p>"),
            ("<p>a &amp; b</p>", "<p>a &amp; b</p>"),
            ("<p>5 &lt; 6</p>", "<p>5 &lt; 6</p>"),
            ("<p>&#65;</p>", "<p>A</p>"),
        ],
        ids=["bare ampersand before a letter", "bare ampersand", "entity", "escaped lt", "charref"],
    )
    def test_ampersands_round_trip_without_gaining_characters(self, raw: str, expected: str) -> None:
        assert email_html.sanitize(raw) == expected

    def test_a_query_string_survives_linkifying(self) -> None:
        """The button-downgrade path produces exactly this shape."""
        cleaned = email_html.sanitize("<p>https://x.test/?a=1&b=2</p>")
        assert 'href="https://x.test/?a=1&amp;b=2"' in cleaned
        assert "&b;" not in cleaned

    @pytest.mark.parametrize(
        ("raw", "expected_href"),
        [
            ("See https://en.wikipedia.org/wiki/Foo_(bar) now", "https://en.wikipedia.org/wiki/Foo_(bar)"),
            ("(see https://x.test/a).", "https://x.test/a"),
            ("go to https://x.test/a.", "https://x.test/a"),
            ("https://x.test/a?b=1!", "https://x.test/a?b=1"),
        ],
        ids=["balanced parens kept", "unbalanced paren dropped", "full stop", "exclamation"],
    )
    def test_linkify_balances_brackets(self, raw: str, expected_href: str) -> None:
        """Stripping every trailing `)` broke the commonest URL shape there is."""
        assert f'href="{expected_href}"' in email_html.sanitize(f"<p>{raw}</p>")

    def test_a_self_closing_anchor_does_not_swallow_the_message(self) -> None:
        """Parsers ignore the slash on a non-void tag, leaving the anchor open."""
        cleaned = email_html.sanitize('<a href="https://evil.test"/>The rest of the email')
        assert cleaned.endswith("The rest of the email")
        assert "</a>The rest" in cleaned

    def test_a_link_gets_noopener_once(self) -> None:
        cleaned = email_html.sanitize('<a href="https://x.test" rel="opener">x</a>')
        assert cleaned.count("rel=") == 1
        assert 'rel="noopener noreferrer"' in cleaned

    def test_a_non_string_is_empty_rather_than_an_exception(self) -> None:
        assert email_html.sanitize(None) == ""
        assert email_html.to_plain_text(12) == ""

    def test_the_body_cap_is_applied(self) -> None:
        assert len(email_html.sanitize("x" * (email_html.MAX_HTML_CHARS + 500))) <= email_html.MAX_HTML_CHARS


class TestHtmlInjectionViaPlaceholders:
    """The acceptance criterion: ``{{first_name}}`` holding markup renders escaped.

    The escaping happens in ``apps.flows.rendering`` (SECURITY-BASELINE §3), and
    what this asserts is that the value survives *composition* still escaped —
    the sanitizer must not "helpfully" unescape it back into live markup.
    """

    def test_an_escaped_value_stays_escaped(self) -> None:
        # Exactly what render(mode="html") produces for a contact whose first
        # name is `<script>alert(1)</script>`.
        rendered = "<p>Hi &lt;script&gt;alert(1)&lt;/script&gt;</p>"
        envelope = envelope_for(text_message(rendered))
        assert "&lt;script&gt;" in envelope.html
        assert "<script>" not in envelope.html


class TestComposition:
    def test_a_from_override_on_the_sending_domain_wins(self) -> None:
        message = OutboundMessage(blocks=(TextBlock(text="<p>x</p>"),), subject="s", from_override="other@sender.test")
        assert envelope_for(message).from_address == "other@sender.test"

    def test_a_from_override_on_another_domain_is_refused(self) -> None:
        """`edit_flows` writes this config; `manage_channels` decides what the channel sends as.

        The two are different permissions (`roles._ADMIN_ONLY_KEYS`), so an
        Editor must not be able to pick a From address on a domain the channel
        was never configured for.
        """
        message = OutboundMessage(blocks=(TextBlock(text="<p>x</p>"),), subject="s", from_override="ceo@bank.test")
        assert envelope_for(message).from_address == "hello@sender.test"

    def test_a_malformed_from_override_falls_back(self) -> None:
        message = OutboundMessage(blocks=(TextBlock(text="<p>x</p>"),), subject="s", from_override="not an address")
        assert envelope_for(message).from_address == "hello@sender.test"

    def test_the_recipient_is_normalised(self) -> None:
        """The address mailed must be the one the suppression gate checked."""
        connection = cast(Any, Connection())
        identity = Identity("  Reader@Example.TEST  ")
        envelope = compose(connection, identity, text_message("<p>x</p>"), unsubscribe_link=UNSUBSCRIBE)
        assert envelope.to == "reader@example.test"

    def test_the_message_id_uses_the_sending_domain(self) -> None:
        assert envelope_for(text_message("<p>x</p>")).message_id.endswith("@sender.test>")

    def test_the_sender_carries_a_display_name(self) -> None:
        assert envelope_for(text_message("<p>x</p>")).sender() == "Sender <hello@sender.test>"

    def test_no_display_name_is_a_bare_address(self) -> None:
        assert envelope_for(text_message("<p>x</p>"), from_name="").sender() == "hello@sender.test"

    def test_a_long_subject_is_truncated_rather_than_refused(self) -> None:
        envelope = envelope_for(text_message("<p>x</p>", subject="s" * 500))
        assert len(envelope.subject) == 300

    def test_url_buttons_become_links(self) -> None:
        message = OutboundMessage(
            blocks=(TextBlock(text="<p>Body</p>"),),
            buttons=(Button(id="b", label="Open it", url="https://example.test/x"),),
            subject="s",
        )
        html = envelope_for(message).html
        assert 'href="https://example.test/x"' in html
        assert "Open it" in html

    def test_a_postback_button_is_downgraded_rather_than_linked(self) -> None:
        """Email has no way to receive a reply, so a postback cannot be a link.

        ``Capabilities.buttons`` is False, so the shared downgrade turns it into
        numbered text before this module ever sees it — which is the behaviour
        under test, not the adapter's own.
        """
        message = OutboundMessage(
            blocks=(TextBlock(text="<p>Body</p>"),),
            buttons=(Button(id="b", label="Press me"),),
            subject="s",
        )
        html = envelope_for(message).html
        assert "href=" not in html.replace(f'href="{UNSUBSCRIBE}"', "")
        assert "Press me" in html

    def test_an_image_block_becomes_an_img(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="https://cdn.test/a.png"),), subject="s")
        assert '<img src="https://cdn.test/a.png"' in envelope_for(message).html

    def test_a_non_image_media_block_becomes_a_link(self) -> None:
        message = OutboundMessage(
            blocks=(MediaBlock(kind="file", url="https://cdn.test/a.pdf", caption="Invoice"),),
            subject="s",
        )
        html = envelope_for(message).html
        assert '<a href="https://cdn.test/a.pdf"' in html
        assert "Invoice" in html

    def test_a_hostile_media_url_is_dropped(self) -> None:
        message = OutboundMessage(blocks=(MediaBlock(kind="image", url="javascript:alert(1)"),), subject="s")
        assert "javascript" not in envelope_for(message).html
