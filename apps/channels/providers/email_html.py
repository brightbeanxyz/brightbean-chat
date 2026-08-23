"""Turning an authored body into the two halves of a multipart email.

Three jobs, in the order a send performs them: sanitize the HTML, derive the
plain-text alternative from what survived, and append the unsubscribe footer to
both.

--------------------------------------------------------------------------
Why sanitize at all, given the renderer already escapes
--------------------------------------------------------------------------

``apps.flows.rendering`` escapes every *substituted value* in ``mode="html"``
(SECURITY-BASELINE §3), so a contact called ``<script>`` cannot reach a mail
client as markup. What it deliberately does not touch is the *template* — the
markup the flow author wrote — because escaping that would deliver an email as
source code.

So the author's own HTML is what this module bounds. That is a smaller threat
than contact input: the author is a workspace member with ``edit_flows``, not a
stranger, and our own UI never renders this string as HTML (the inbox escapes
message bodies — L4-D's hostile-content suite). The reasons to sanitize anyway
are the ordinary ones for email:

* a ``<script>`` or an ``onclick=`` is dead weight in every mail client and a
  spam signal in most, and ``graph_json`` can be hand-edited past the builder;
* ``href``/``src`` schemes must be bounded, because ``javascript:`` in a link is
  live in a *webmail* client even though it is inert in a desktop one;
* the plain-text alternative is derived from the same tree, so one parse gives
  both halves and they cannot disagree about what the message said.

A stdlib allowlist rather than ``nh3``/``bleach``: the allowlist below is small
and closed, this is not the boundary that stops attacker content (the renderer
is), and a dependency-free implementation keeps the audit job's surface where it
is. The parser is ``html.parser``, which is a tokenizer — it never executes
anything and never builds a DOM.

--------------------------------------------------------------------------
Allowlist, not blocklist
--------------------------------------------------------------------------

Unknown tags are **unwrapped** rather than dropped: an unrecognised ``<section>``
loses its markup and keeps its words, which is the direction that fails safe for
a message somebody wrote. Unknown attributes are dropped outright, which is the
opposite direction and also the safe one — an attribute is never content.
"""

import re
from html import escape, unescape
from html.parser import HTMLParser
from typing import Any

from apps.common.validators import is_renderable_url

__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_TAGS",
    "MAX_HTML_CHARS",
    "sanitize",
    "text_footer",
    "to_plain_text",
    "with_unsubscribe_footer",
]

#: Tags an email body may keep. Chosen for what mail clients actually render
#: consistently: no ``<form>``, no ``<iframe>``, no ``<object>``, and no
#: ``<style>`` — a client that honours a stylesheet is the one that would honour
#: ``expression()`` with it, and inline ``style`` (below) covers the formatting
#: the editor produces.
ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "i",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)

#: Tags whose *contents* go with them. Everything else that is not allowed is
#: unwrapped; these carry no prose, so keeping their text would paste a script
#: body or a stylesheet into the message.
_DROP_WITH_CONTENT: frozenset[str] = frozenset({"script", "style", "template", "title"})

#: Tags that never have a closing partner.
_VOID_TAGS: frozenset[str] = frozenset({"br", "hr", "img"})

#: Per-tag attribute allowlist. ``None`` means "no attributes at all", which is
#: the default for anything not named here.
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title", "rel", "target"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "align"}),
    "table": frozenset({"width", "cellpadding", "cellspacing", "border"}),
}

#: Attributes carrying a URL, checked against ``is_renderable_url`` plus the one
#: scheme that check does not know about because it is not a *renderable* URL.
_URL_ATTRIBUTES: frozenset[str] = frozenset({"href", "src"})

#: ``mailto:`` is legitimate in an email body and is not an ``href`` a browser
#: would fetch, so ``apps.common.validators.is_renderable_url`` — which exists to
#: answer "may this become a ``src``?" — correctly refuses it. Allowed here, with
#: the same control-character rule applied.
_MAILTO_RE = re.compile(r"^mailto:[^\s<>\"'\x00-\x20\x7f]+$", re.IGNORECASE)

#: Numeric attributes a mail client reads as a dimension. Anything else in them
#: is dropped rather than passed through, because ``width="1" onload="…"`` is not
#: how attributes parse but ``width='1" onload="x'`` in a hand-edited graph is.
_NUMERIC_ATTRIBUTES: frozenset[str] = frozenset(
    {"width", "height", "colspan", "rowspan", "border", "cellpadding", "cellspacing"}
)

_ALIGNMENTS: frozenset[str] = frozenset({"left", "right", "center", "justify"})

#: Hard bound on what this module will process, mirroring
#: ``Capabilities.max_text_len`` for email and the ``html_body`` schema cap. The
#: renderer already truncates to the same number; this is the backstop for a
#: body that reached here another way.
MAX_HTML_CHARS = 100_000

#: A bare URL in a text node. Deliberately conservative: it stops at whitespace
#: and at the punctuation that ends a sentence rather than a URL, so
#: ``see https://x.test/a.`` does not swallow the full stop. Written without a
#: nested quantifier — this runs over message bodies, and a regex that
#: backtracks badly on one is a denial-of-service primitive
#: (``apps/common/logging.py`` makes the same point about its own patterns).
_BARE_URL = re.compile(r"\bhttps?://[^\s<>\"']{2,2000}")

#: Trailing characters that end a sentence rather than a URL. Closing brackets
#: are handled separately, by :func:`_trim_url`, because whether one belongs to
#: the URL depends on what came before it.
_URL_TRAILING = ".,;:!?'\""

#: Closing bracket -> its opening partner. A trailing ``)`` is only punctuation
#: when the URL does not open one itself.
_URL_BRACKETS = {")": "(", "]": "[", "}": "{"}

#: Block-level tags that become a line break in the text alternative.
_TEXT_BREAKS: frozenset[str] = frozenset(
    {"blockquote", "div", "h1", "h2", "h3", "h4", "hr", "li", "ol", "p", "pre", "table", "tr", "ul"}
)


class _Sanitizer(HTMLParser):
    """Rebuild the document from the tokens that survive the allowlist."""

    def __init__(self) -> None:
        # convert_charrefs=True: the parser resolves character references into
        # text, and :meth:`handle_data` escapes on the way out, so the document
        # round-trips through one decode/encode pair.
        #
        # The hand-rolled alternative — convert_charrefs=False plus
        # ``handle_entityref``/``handle_charref`` re-emitting ``f"&{name};"`` —
        # looked like it preserved the author's markup and instead corrupted it.
        # ``html.parser`` reports ``&b`` in ``?a=1&b=2`` as an entityref named
        # "b" even though no semicolon followed it, so re-emitting with one
        # turned an ordinary query string into ``&b;=2``: wrong visible text and
        # a truncated link, on the commonest input there is. Letting the parser
        # decode and escaping once is the version that cannot invent characters.
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        #: Tags opened and kept, so a stray ``</p>`` for a tag we unwrapped does
        #: not close something it never opened.
        self._open: list[str] = []
        #: Depth inside a drop-with-content tag. A counter rather than a flag
        #: because ``<script><script>`` is a document a parser must survive.
        self._suppressed = 0
        #: Depth inside an ``<a>``, for the same reason: nested anchors are not
        #: valid HTML and a client renders the result unpredictably.
        self._in_anchor = 0

    # -- tokens -------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_WITH_CONTENT:
            self._suppressed += 1
            return
        if self._suppressed or tag not in ALLOWED_TAGS:
            return
        rendered = _attributes(tag, attrs)
        if tag in _VOID_TAGS:
            self.parts.append(f"<{tag}{rendered} />")
            return
        if tag == "a":
            self._in_anchor += 1
        self._open.append(tag)
        self.parts.append(f"<{tag}{rendered}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """``<tag/>``. Only void elements may actually close themselves.

        HTML parsers ignore a trailing slash on a non-void element, so emitting
        ``<a href="…" />`` left the anchor **open** and every remaining word of
        the message became part of that link — and because this path never
        pushed to ``_open``, :meth:`result`'s balancing tail could not close it
        either. A non-void tag is written as an explicit empty pair instead.
        """
        if self._suppressed or tag in _DROP_WITH_CONTENT or tag not in ALLOWED_TAGS:
            return
        rendered = _attributes(tag, attrs)
        if tag in _VOID_TAGS:
            self.parts.append(f"<{tag}{rendered} />")
            return
        self.parts.append(f"<{tag}{rendered}></{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_WITH_CONTENT:
            self._suppressed = max(0, self._suppressed - 1)
            return
        if self._suppressed or tag in _VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag not in self._open:
            # A close with no matching open. Emitting it would let a hand-edited
            # body close a tag this module wrote around it.
            return
        # Close everything the author left open inside it, innermost first, so
        # the output is balanced whatever the input was.
        while self._open:
            current = self._open.pop()
            if current == "a":
                self._in_anchor = max(0, self._in_anchor - 1)
            self.parts.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._suppressed:
            return
        escaped = escape(data, quote=False)
        # A bare URL in the body becomes a link. That is what makes the shared
        # downgrade's output usable here: ``Capabilities.buttons`` is False for
        # email, so ``apps.channels.downgrade`` inlines every URL button as
        # ``label: url`` text before the adapter sees it — correct, because an
        # email has no button widget — and this is what turns that text back
        # into something clickable, which is what ``url_buttons=True`` on the
        # capability row means for a channel whose buttons are hyperlinks.
        #
        # Text nodes only, and never inside an existing anchor, so a link the
        # author already wrote is never nested inside a second one.
        self.parts.append(escaped if self._in_anchor else _linkify(escaped))

    # Comments, declarations and processing instructions are dropped entirely.
    # A conditional comment is how a body smuggles markup past a naive filter,
    # and nothing in an email needs one.
    def handle_comment(self, data: str) -> None:
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def result(self) -> str:
        tail = "".join(f"</{tag}>" for tag in reversed(self._open))
        self._open.clear()
        self._in_anchor = 0
        return "".join(self.parts) + tail


def _linkify(escaped: str) -> str:
    """Wrap bare URLs in a text node with anchors. Input is already escaped.

    Operates on escaped text, so the URL that goes into the ``href`` is the
    escaped form too — which is the correct thing to put in an attribute, and
    means this cannot reintroduce markup the sanitizer just removed.
    """

    def _wrap(match: "re.Match[str]") -> str:
        url, trailing = _trim_url(match.group(0))
        if not url:
            return match.group(0)
        return f'<a href="{url}" rel="noopener noreferrer">{url}</a>{trailing}'

    return _BARE_URL.sub(_wrap, escaped)


def _trim_url(url: str) -> tuple[str, str]:
    """Split a matched URL into the URL and the punctuation that followed it.

    Balanced brackets stay in. ``https://en.wikipedia.org/wiki/Foo_(bar)`` is
    the shape that makes this necessary: stripping every trailing ``)`` broke
    the closing paren off a URL that opened one, and the link 404s. A ``)`` is
    only punctuation when there is no unclosed ``(`` to its left.
    """
    trailing = ""
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            trailing = last + trailing
            url = url[:-1]
            continue
        opener = _URL_BRACKETS.get(last)
        if opener is not None and url.count(opener) < url.count(last):
            trailing = last + trailing
            url = url[:-1]
            continue
        break
    return url, trailing


def _attributes(tag: str, attrs: list[tuple[str, str | None]]) -> str:
    """The attributes this tag may keep, rendered back into the start tag."""
    allowed = ALLOWED_ATTRIBUTES.get(tag, frozenset())
    out: list[str] = []
    seen: set[str] = set()
    has_href = False
    for raw_name, raw_value in attrs:
        name = (raw_name or "").lower()
        # `seen` because a hand-edited body may repeat an attribute, and the
        # last one wins in a browser while the first would win in a naive
        # rebuild. Keeping the first and dropping the rest makes the two agree.
        if name not in allowed or name in seen:
            continue
        value = _attribute_value(name, raw_value or "")
        if value is None:
            continue
        seen.add(name)
        has_href = has_href or name == "href"
        out.append(f' {name}="{escape(value, quote=True)}"')
    if tag == "a" and has_href:
        # Every link in an email opens outside our origin, and an author-supplied
        # `rel` was dropped above, so this is the only one. `noopener` is the part
        # that matters: a webmail client renders this inside a frame it owns.
        out.append(' rel="noopener noreferrer"')
    return "".join(out)


def _attribute_value(name: str, raw: str) -> str | None:
    """The value to keep, or ``None`` to drop the attribute."""
    value = raw.strip()
    if not value:
        return None
    if name in _URL_ATTRIBUTES:
        # Entities first: ``&#106;avascript:`` is the same URL to a client and a
        # different string to a naive prefix check.
        resolved = unescape(value).strip()
        if is_renderable_url(resolved) or _MAILTO_RE.match(resolved):
            return resolved
        return None
    if name in _NUMERIC_ATTRIBUTES:
        digits = value.removesuffix("%")
        return value if digits.isdigit() else None
    if name == "align":
        return value.lower() if value.lower() in _ALIGNMENTS else None
    if name == "target":
        return "_blank" if value.lower() == "_blank" else None
    if name == "rel":
        # Rebuilt by _attributes; an author-supplied rel is not passed through.
        return None
    return value


def sanitize(html: Any) -> str:
    """The author's HTML, reduced to the allowlist. Never raises.

    A non-string, or anything the parser chokes on, yields ``""`` rather than an
    exception: this runs on the send path, and a body that cannot be cleaned is
    a message that does not go out, not a worker that dies.
    """
    if not isinstance(html, str) or not html:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(html[:MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient, but never trusted to be
        return ""
    return parser.result()


class _TextExtractor(HTMLParser):
    """The plain-text alternative, from the same tokens the sanitizer kept."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._href: str = ""
        self._link_text: list[str] = []
        self._in_link = False
        self._list_markers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._in_link = True
            self._link_text = []
            self._href = next((value or "" for name, value in attrs if (name or "").lower() == "href"), "")
            return
        if tag == "br":
            self.parts.append("\n")
            return
        if tag == "hr":
            self.parts.append("\n---\n")
            return
        if tag in {"ul", "ol"}:
            self._list_markers.append("1" if tag == "ol" else "-")
            self.parts.append("\n")
            return
        if tag == "li":
            marker = self._list_markers[-1] if self._list_markers else "-"
            self.parts.append(f"\n{'-' if marker == '-' else '*'} ")
            return
        if tag in _TEXT_BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            text = "".join(self._link_text).strip()
            self._in_link = False
            # ``text <url>`` is the convention every mail client's own
            # html-to-text does, and it keeps the destination visible in a
            # client that shows only the text part.
            if text and self._href and text != self._href:
                self.parts.append(f"{text} <{self._href}>")
            else:
                self.parts.append(text or self._href)
            self._link_text = []
            self._href = ""
            return
        if tag in {"ul", "ol"} and self._list_markers:
            self._list_markers.pop()
            self.parts.append("\n")
            return
        if tag in _TEXT_BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text.append(data)
            return
        self.parts.append(data)

    def result(self) -> str:
        text = "".join(self.parts)
        # Collapse runs of blank lines to one, and trailing spaces away. A
        # message that was one <p> per line should not arrive double-spaced.
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def to_plain_text(html: Any) -> str:
    """The auto-generated ``text/plain`` alternative (SPEC §6.7). Never raises.

    Derived from the HTML rather than authored separately, because SPEC says the
    alternative is generated and because two authored bodies is two things that
    can disagree about what the message said.
    """
    if not isinstance(html, str) or not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html[:MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - same reasoning as sanitize()
        return ""
    return parser.result()


#: The footer's wording. One sentence, because a footer that argues is a footer
#: people report as spam instead of clicking.
FOOTER_TEXT = "Don't want these emails?"
FOOTER_LINK_TEXT = "Unsubscribe"


def text_footer(url: str) -> str:
    return f"{FOOTER_TEXT} {FOOTER_LINK_TEXT}: {url}"


def with_unsubscribe_footer(html: str, text: str, url: str) -> tuple[str, str]:
    """Append the hosted unsubscribe link to both halves (SPEC §6.7).

    **Not optional and not configurable.** SPEC §6.7 puts the footer "in core"
    alongside the ``List-Unsubscribe`` header, and the adapter appends it after
    the node has finished rendering — so it is on a broadcast, an inbox reply
    and an API send too, not only on the one path whose config could have
    offered a checkbox. There is deliberately nothing to switch it off with.

    The URL is minted by this deployment (``apps.channels.unsubscribe``), so it
    is not escaped as untrusted input — but it is escaped as *markup*, because
    it lands inside an attribute and an unescaped ``&`` between query parameters
    is malformed HTML in any case.
    """
    safe_url = escape(url, quote=True)
    footer_html = (
        f'<p><a href="{safe_url}" rel="noopener noreferrer">{FOOTER_LINK_TEXT}</a> '
        f"&mdash; {escape(FOOTER_TEXT, quote=False)}</p>"
    )
    joined_html = f"{html}\n{footer_html}" if html else footer_html
    joined_text = f"{text}\n\n{text_footer(url)}" if text else text_footer(url)
    return joined_html, joined_text
