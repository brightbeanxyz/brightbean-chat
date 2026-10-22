"""The starter templates shipped in the repository, and how they are found.

``flow-templates/`` at the repository root holds flow templates anybody can
import — the seed of the shared library the issue describes, and the directory a
community pull request adds to. A test validates every file in it against the
importer and imports each into a clean workspace, so a template that stops
working is a red build rather than a download that fails for a stranger.

**Not** ``templates/``: that path is Django's own ``TEMPLATES["DIRS"]``
(``config/settings/base.py``), and putting JSON documents inside the HTML
template loader's search path would be a trap for the next person who wonders
why their template name resolves to a flow.

--------------------------------------------------------------------------
Cards, and why the description is not in the file
--------------------------------------------------------------------------

The gallery needs a sentence of English and a category per template, and the
envelope has a field for neither. It does not get one. ``f.obj`` sets
``additionalProperties: false`` on every object it builds and offers no opt-out
(``apps/flows/schema/fields.py``), so a document carrying a ``meta`` key is
refused by **every installation running today's release** — and these files are
downloaded from a repository and uploaded into other people's installs. It would
also break the byte-exact export round trip, since ``export_document`` would
never emit the key back.

So :class:`TemplateCard` **derives** every fact from the validated document —
the name from the entry flow, the platforms from the manifest's ``platform``
keys, the other requirement kinds from the rest of it — and the only
hand-written thing is :data:`TEMPLATE_COPY`, the one part no machine can infer.
Nothing that could drift is duplicated: the name on a card *is* the document's
name, and the badges *are* the manifest. Human copy in a module-level dict is
the house pattern already (``REQUIREMENT_KIND_LABELS`` in
``apps/flows/portability/cards.py``, ``FILTER_ICONS`` in
``apps/common/templatetags/common_extras.py``).

A file with no copy entry still gets a card with an empty summary, so a
self-hoster's drop-in is never invisible; a test asserts the two sets match both
ways so *ours* can never go missing.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

__all__ = [
    "LIBRARY_RELATIVE_PATH",
    "STARTER_CATEGORY",
    "gallery_entries",
    "SLUG_PATTERN",
    "TEMPLATE_COPY",
    "TemplateCard",
    "TemplateCopy",
    "library_path",
    "read_template",
    "template_card",
    "template_cards",
    "template_for_slug",
    "template_paths",
]

logger = logging.getLogger(__name__)

#: Where the shipped templates live, relative to the repository root.
LIBRARY_RELATIVE_PATH = Path("flow-templates")

#: What a filename's stem has to look like to be addressable as a URL segment.
#: Django's ``<slug:…>`` converter is the first gate and this is the second; a
#: test asserts every shipped filename passes, so a file the URL could never
#: reach is a red build rather than a card whose button 404s.
#:
#: ``\Z`` rather than ``$``: Python's ``$`` also matches immediately *before* a
#: trailing newline, so ``$`` here would accept ``"some-template\n"``. The stem
#: comparison in :func:`template_for_slug` would still refuse it, but a guard
#: whose whole job is refusing what does not belong should not need the next
#: check to cover for it.
SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


def library_path() -> Path:
    """Absolute path of the shipped template directory."""
    from django.conf import settings

    return Path(settings.BASE_DIR) / LIBRARY_RELATIVE_PATH


def template_paths() -> list[Path]:
    """Every shipped template, in a stable order.

    Sorted rather than in directory order: a test that iterates these reports
    its failures in the same sequence on every machine, and a filesystem's idea
    of order is not one.
    """
    directory = library_path()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def read_template(path: Path) -> tuple[dict[str, Any] | None, list[Any]]:
    """One shipped template, through the same front door an upload uses.

    Deliberately :func:`apps.flows.portability.imports.parse_and_validate` and
    not a shortcut: a file this repository ships gets exactly the scrutiny a
    stranger's does, which is what makes "CI validates the templates against the
    importer" a true statement rather than a weaker one about a different code
    path.
    """
    from apps.flows.portability.imports import parse_and_validate

    return parse_and_validate(path.read_bytes())


@dataclass(frozen=True)
class TemplateCopy:
    """The three things about a template a machine cannot work out for itself.

    :attr:`title` is optional and the odd one out: the gallery's whole point is
    that a card's name *is* the document's own name, so nothing about it can
    drift out of step. Overriding it here exists for exactly one reason —
    translating that name — and a slug with no entry, or an entry with no
    title, still gets the document's own name, in whatever language the
    document itself is written in.
    """

    category: str | Promise
    summary: str | Promise
    title: str | Promise | None = None


@dataclass(frozen=True)
class TemplateCard:
    """One shipped template, as the gallery shows it.

    Everything but :attr:`summary`, :attr:`category` and a translated
    :attr:`name` is read off the validated document, so a card cannot disagree
    with the file it describes.
    """

    slug: str
    filename: str
    name: str | Promise
    summary: str | Promise
    category: str | Promise
    platforms: tuple[str, ...]
    needs: tuple[str, ...]
    trigger_types: tuple[str, ...]
    flow_count: int
    step_count: int
    #: The template makes an outbound HTTP request when it runs. Worth saying on
    #: the card because it is the one thing installing a template does *on your
    #: behalf, to somebody else* — the rest of it only touches your workspace.
    calls_out: bool


#: The category for the handful of templates a first-time workspace is shown.
#: Named because two places select on it and a bare string in the second would
#: go quietly wrong if this dict were ever re-worded: the gallery's filter chips
#: and ``apps.flows.views._featured``.
STARTER_CATEGORY = _("Starters")

#: Keyed by filename stem. A template with no entry still gets a card; a stale
#: entry and a missing one are both caught by a test that compares this against
#: the directory in both directions.
TEMPLATE_COPY: dict[str, TemplateCopy] = {
    "collect-an-email-address": TemplateCopy(
        category=_("Grow"),
        title=_("Ask for an email address"),
        summary=_("Asks for an email address, checks it looks real, and saves it to the contact before replying."),
    ),
    "event-reminder": TemplateCopy(
        category=_("Engage"),
        title=_("Remind people about an event"),
        summary=_("Signs someone up for an event over Telegram, then messages them again nearer the time."),
    ),
    "feedback-after-a-purchase": TemplateCopy(
        category=_("Engage"),
        title=_("Ask how it went after a purchase"),
        summary=_("Waits until the order has landed, asks how it went, and routes the answer by what they say."),
    ),
    "first-message-welcome": TemplateCopy(
        category=_("Starters"),
        title=_("Welcome someone new"),
        summary=_("The first thing a new Telegram contact hears, with a follow-up a little later."),
    ),
    "follow-up-an-unanswered-enquiry": TemplateCopy(
        category=_("Convert"),
        title=_("Follow up an enquiry that went quiet"),
        summary=_(
            "Chases an enquiry that went quiet twice, spaced out, then tags it so you can see who never replied."
        ),
    ),
    "hand-over-to-a-person": TemplateCopy(
        category=_("Engage"),
        title=_("Hand over to a person"),
        summary=_("Takes the conversation off automation and assigns it to a teammate, with a note saying why."),
    ),
    "instagram-comment-affiliate-picks": TemplateCopy(
        category=_("Convert"),
        title=_("Affiliate picks from comments"),
        summary=_(
            "A comment on the post sends a swipeable gallery of your affiliate picks, each card linking straight out."
        ),
    ),
    "instagram-comment-follow-to-unlock": TemplateCopy(
        category=_("Grow"),
        title=_("Follow to unlock"),
        summary=_("Ask for the follow before you hand over the freebie, then tag whoever confirms."),
    ),
    "instagram-comment-link-in-dm": TemplateCopy(
        category=_("Convert"),
        title=_("Auto-DM the link from comments"),
        summary=_("Someone comments, you reply publicly and DM them the link. The classic comment-to-DM."),
    ),
    "instagram-comment-product-gallery": TemplateCopy(
        category=_("Convert"),
        title=_("Product lineup in DMs"),
        summary=_("Send the whole lineup as a gallery, then answer the two questions that stop a sale."),
    ),
    "instagram-comment-reel-to-product": TemplateCopy(
        category=_("Convert"),
        title=_("Sell from Reel comments"),
        summary=_("A Reel got people asking. DM them the product and tag the interest."),
    ),
    "instagram-comment-rsvp": TemplateCopy(
        category=_("Convert"),
        title=_("Comments into RSVPs"),
        summary=_(
            "Turn \u201ccomment to join\u201d into a confirmed RSVP with a calendar link and a tag to broadcast to later."
        ),
    ),
    "instagram-comment-to-discount-code": TemplateCopy(
        category=_("Convert"),
        title=_("Comment for a discount code"),
        summary=_("A comment sends the discount code by DM, then follows up in case it went unused."),
    ),
    "instagram-comment-to-dm-lead-magnet": TemplateCopy(
        category=_("Starters"),
        title=_("Comment-to-DM lead magnet"),
        summary=_("Comment-triggered DM that delivers a guide and collects an email address."),
    ),
    "instagram-default-reply-autoresponder": TemplateCopy(
        category=_("Engage"),
        title=_("Respond to every DM"),
        summary=_(
            "Catch every DM nothing else answered, greet people by name and route them to buy, ask or reach a person."
        ),
    ),
    "instagram-keyword-course-early-access": TemplateCopy(
        category=_("Convert"),
        title=_("Early access to the launch"),
        summary=_("A keyword puts people on the launch waitlist and tags them for the broadcast on the day."),
    ),
    "instagram-keyword-dm-to-sms": TemplateCopy(
        category=_("Grow"),
        title=_("Move the conversation to SMS"),
        summary=_(
            "Move the conversation to SMS before Instagram's 24-hour window closes, with a DM fallback if the text fails."
        ),
    ),
    "instagram-keyword-email-capture": TemplateCopy(
        category=_("Grow"),
        title=_("Grow the email list"),
        summary=_("Trade a download for an email address. The answer records consent alongside it."),
    ),
    "instagram-keyword-faq-hub": TemplateCopy(
        category=_("Engage"),
        title=_("Answer the usual questions"),
        summary=_("One keyword, one hub, four answers, and a way through to a person."),
    ),
    "instagram-keyword-link-drop": TemplateCopy(
        category=_("Engage"),
        title=_("Drop the link in DMs"),
        summary=_("The simplest one: a keyword in the DM, the link straight back."),
    ),
    "instagram-keyword-qualify-quiz": TemplateCopy(
        category=_("Engage"),
        title=_("Qualify with a quiz"),
        summary=_("Two questions that tag people by where they are, then send each group a different offer."),
    ),
    "instagram-keyword-sms-list": TemplateCopy(
        category=_("Grow"),
        title=_("Grow the SMS list"),
        summary=_("Collect phone numbers with the consent wording the SMS rules expect."),
    ),
    "instagram-keyword-where-is-this-from": TemplateCopy(
        category=_("Engage"),
        title=_("Answer 'where is this from?'"),
        summary=_("Answer \u201cwhere is this from?\u201d the moment it lands, with the product and a link."),
    ),
    "instagram-keyword-youtube-subscribe": TemplateCopy(
        category=_("Grow"),
        title=_("Send people to YouTube"),
        summary=_("Send your Instagram audience to the long version on YouTube."),
    ),
    "instagram-link-in-bio-capture": TemplateCopy(
        category=_("Grow"),
        title=_("Link in bio opens a chat"),
        summary=_("The link in your bio opens a chat that already knows where they came from."),
    ),
    "instagram-price-question": TemplateCopy(
        category=_("Convert"),
        title=_("Answer price questions on Instagram"),
        summary=_("Answers “how much?” in the comments by DM, and asks what they are after before quoting."),
    ),
    "instagram-story-collab-requests": TemplateCopy(
        category=_("Grow"),
        title=_("Sort collab requests from Stories"),
        summary=_("Sort collab replies into brands and creators, capture a brief and tag the request."),
    ),
    "instagram-story-limited-time-offer": TemplateCopy(
        category=_("Convert"),
        title=_("Limited-time offer from Stories"),
        summary=_("Send the code from your Story, then nudge once before the window closes."),
    ),
    "instagram-story-mention-thank-you": TemplateCopy(
        category=_("Engage"),
        title=_("Thank someone for a story mention"),
        summary=_("Thanks somebody who mentioned you in a story, and comes back later with an offer."),
    ),
    "instagram-story-reply-to-conversation": TemplateCopy(
        category=_("Engage"),
        title=_("Story reply starts a conversation"),
        summary=_("Turns a story reply into a real conversation instead of a notification you never answer."),
    ),
    "messenger-comment-to-dm": TemplateCopy(
        category=_("Convert"),
        title=_("Reply to a Facebook comment privately"),
        summary=_("Replies to a Facebook comment privately, which is the reply that can actually ask for something."),
    ),
    "messenger-quote-request": TemplateCopy(
        category=_("Convert"),
        title=_("Collect the details for a quote"),
        summary=_("Collects the three details you need to quote, one question at a time, and saves each one."),
    ),
    "messenger-welcome": TemplateCopy(
        category=_("Starters"),
        title=_("Messenger welcome"),
        summary=_("What a first-time Messenger contact hears, in four short messages rather than one wall."),
    ),
    "out-of-hours-reply": TemplateCopy(
        category=_("Engage"),
        title=_("Reply outside opening hours"),
        summary=_("Answers outside opening hours with when you are next open, so nobody is left waiting."),
    ),
    "sms-appointment-reminder": TemplateCopy(
        category=_("Engage"),
        title=_("Remind someone of an appointment"),
        summary=_("Texts a reminder before the appointment, and again when it is close."),
    ),
    "sms-keyword-opt-in": TemplateCopy(
        category=_("Starters"),
        title=_("SMS keyword opt-in"),
        summary=_("Keyword opt-in over SMS that records consent and tags the subscriber."),
    ),
    "sms-review-request": TemplateCopy(
        category=_("Engage"),
        title=_("Ask for a review"),
        summary=_("Waits a few days, asks for a review, and stops asking the people who already left one."),
    ),
    "telegram-booking-enquiry": TemplateCopy(
        category=_("Convert"),
        title=_("Take a booking enquiry"),
        summary=_("Takes a booking enquiry over Telegram and saves the date, the size and the contact."),
    ),
    "telegram-support-triage": TemplateCopy(
        category=_("Engage"),
        title=_("Sort support messages"),
        summary=_("Sorts an incoming support message into the right queue and tags it for whoever picks it up."),
    ),
    "telegram-welcome-and-faq": TemplateCopy(
        category=_("Starters"),
        title=_("Telegram welcome and FAQ"),
        summary=_("Telegram welcome message with a three-way FAQ menu behind quick replies."),
    ),
    "waitlist-signup": TemplateCopy(
        category=_("Grow"),
        title=_("Join a waitlist"),
        summary=_("Collects an email for the waitlist and tags the person so you can message the list later."),
    ),
    "whatsapp-opening-hours": TemplateCopy(
        category=_("Engage"),
        title=_("Answer when you are open"),
        summary=_("Answers “are you open?” with the real answer for the day it is asked."),
    ),
    "whatsapp-order-status": TemplateCopy(
        category=_("Engage"),
        title=_("Check an order"),
        summary=_("Asks for the order number, looks it up, and says where it is."),
    ),
}


def template_for_slug(slug: str) -> Path | None:
    """The shipped template a URL segment names, or ``None``.

    **Never builds a path from the argument.** The candidate set comes from
    :func:`template_paths`, and ``slug`` is only ever compared for equality
    against a stem this module produced — so ``..``, an absolute path, a URL-
    encoded separator and a symlink all have nothing to act on, rather than being
    sanitised away and hoped about. Django's ``<slug:…>`` converter means most of
    those never reach the view at all; :data:`SLUG_PATTERN` stops the rest before
    the scan.
    """
    if not SLUG_PATTERN.match(slug or ""):
        return None
    return next((path for path in template_paths() if path.stem == slug), None)


def template_card(path: Path) -> TemplateCard | None:
    """One template as a card, or ``None`` if the file cannot be turned into one.

    ``None`` rather than an exception, for **both** halves of "cannot": a file
    that does not validate and a file that does not read. The gallery walks every
    file in a directory a self-hoster can drop things into, so one bad drop-in
    has to cost that template its card and nothing else — guarding only the parse
    would still let a permission bit or a disk error take down the whole page. A
    *shipped* file failing either way is our bug, which is why both are logged.
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        logger.warning("flow template %s could not be read and has no card: %s", path.name, error)
        return None
    return _card(str(path), hashlib.blake2b(raw, digest_size=16).hexdigest())


def template_cards() -> list[TemplateCard]:
    """Every shipped template as a card, in :func:`template_paths` order."""
    return [card for card in (template_card(path) for path in template_paths()) if card is not None]


@lru_cache(maxsize=256)
def _card(path_str: str, content_digest: str) -> TemplateCard | None:
    """The parse behind :func:`template_card`, memoized on the file's *content*.

    Twenty-odd ``parse_and_validate`` calls per page view is the kind of cost
    that only shows up under load, so the result is cached until the file
    changes. The key is a digest of the bytes rather than ``(mtime, size)``: an
    edit that keeps a file the same length — swapping a placeholder URL for one
    of equal length, fixing a typo — is invisible to mtime granularity on a
    filesystem that rounds it, and a stale card would then outlive the file it
    describes. Hashing costs microseconds against a full schema validation.

    Keyed on the path *string* as well, so a test pointing ``settings.BASE_DIR``
    at a ``tmp_path`` gets its own entries and needs no cache-clearing fixture.

    The re-read here is the price of ``lru_cache`` keying on every argument —
    passing the bytes in would cache up to ``MAX_DOCUMENT_BYTES`` per entry. It
    happens on a miss only, and it is guarded because the file can vanish between
    the digest and the parse.
    """
    path = Path(path_str)
    try:
        document, issues = read_template(path)
    except OSError as error:
        logger.warning("flow template %s could not be read and has no card: %s", path.name, error)
        return None
    if document is None:
        logger.warning(
            "flow template %s does not validate and has no card: %s",
            path.name,
            "; ".join(issue.message for issue in issues[:3]),
        )
        return None

    flows = document["flows"]
    entry = next((flow for flow in flows if flow["key"] == document["entry"]), flows[0])
    requirements = document["requirements"]
    copy = TEMPLATE_COPY.get(path.stem)

    return TemplateCard(
        slug=path.stem,
        filename=path.name,
        name=copy.title if copy and copy.title else entry["name"],
        summary=copy.summary if copy else "",
        category=copy.category if copy else entry.get("folder", ""),
        platforms=tuple(item["key"] for item in requirements.get("platform", [])),
        # Sorted for a stable badge order; ``platform`` is dropped because it is
        # already its own row on the card.
        needs=tuple(sorted(kind for kind, items in requirements.items() if items and kind != "platform")),
        trigger_types=tuple(dict.fromkeys(t["type"] for flow in flows for t in flow["triggers"])),
        flow_count=len(flows),
        step_count=sum(_steps_in(flow["graph"]) for flow in flows),
        calls_out=_calls_out(document),
    )


def _calls_out(document: dict[str, Any]) -> bool:
    """Whether running this template makes an outbound HTTP request.

    Both halves are needed. ``request_header`` in the manifest catches a template
    whose call carries a header the importer must ask for, and the node sweep
    catches one that calls out with no header at all — an export strips header
    *values*, so a template can legitimately arrive with an ``external_request``
    node and nothing in ``requirements`` to show for it.
    """
    if document["requirements"].get("request_header"):
        return True
    return any(
        node.get("type") == "external_request" for flow in document["flows"] for node in flow["graph"].get("nodes", [])
    )


def _steps_in(graph: dict[str, Any]) -> int:
    """Nodes that actually run.

    ``note`` nodes are ``annotation=True``: they take no part in routing and are
    excluded from the entry-node count (``_routable`` in
    ``apps/flows/schema/validation.py``). Counting them would tell somebody a
    one-message template is two steps long, which is exactly the templates that
    ship a note — all of them.
    """
    from apps.flows.schema import node_spec

    return sum(1 for node in graph["nodes"] if not (spec := node_spec(node["type"])) or not spec.annotation)


# ---------------------------------------------------------------------------
# The compact tile shape
# ---------------------------------------------------------------------------


def gallery_entries() -> list[dict[str, Any]]:
    """The shipped templates as the Home and flow-list tiles read them.

    A projection of :func:`template_cards`, not a second walk of the directory.
    The tiles want five facts and the gallery card wants nine, but there is only
    one library and only one definition of what a card says about it — deriving
    the smaller shape from the larger is what stops the two pages disagreeing
    about how many templates ship.

    Cached on the directory's own fingerprint rather than recomputed. Home and
    the import review page both call this per render, and ``template_cards()``
    reads and digests every file on disk *before* it can consult the per-card
    cache — so without this the two most-visited pages in the product paid
    forty-odd file reads each time. Keyed on (name, mtime, size) per file rather
    than held for the life of the process, so a template edited on disk still
    shows through, which the library's own cache guarantees and the tests rely
    on.

    Dicts rather than the dataclass because the tile template indexes them and
    a caller may sort or slice, which a shared frozen instance would make
    everybody's business.
    """
    # A fresh dict *and* a fresh platforms list per call: dict() is shallow, so
    # handing back the cached list would let one caller's sort or append reach
    # every later render.
    return [{**entry, "platforms": list(entry["platforms"])} for entry in _gallery_entries(_library_fingerprint())]


def _library_fingerprint() -> tuple[Any, ...]:
    """What the gallery cache keys on: every file's name, mtime and size.

    Cheap next to reading and hashing each file, and it moves for the edits that
    matter — a new template, a deleted one, or a changed one. A same-size
    same-mtime rewrite is invisible here, which is exactly the case
    :func:`_card` keys on a content digest to catch; this cache sits in front of
    that one rather than replacing it.
    """
    entries: list[Any] = []
    for path in template_paths():
        try:
            stat = path.stat()
        except OSError:
            entries.append((path.name, None))
        else:
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


@lru_cache(maxsize=4)
def _gallery_entries(fingerprint: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
    """The projection itself, computed once per state of the directory."""
    return tuple(
        {
            "slug": card.slug,
            "name": card.name,
            "category": card.category,
            "platforms": tuple(card.platforms),
            "platform_label": _platform_label(list(card.platforms)),
            "steps": card.step_count,
        }
        for card in template_cards()
    )


def _platform_label(platforms: list[str]) -> str:
    """ "instagram" -> "Instagram"; two -> "Instagram and Messenger"; none -> "Any channel"."""
    from apps.common.platforms import Platform

    names = []
    for platform in platforms:
        try:
            names.append(str(Platform(platform).label))
        except ValueError:
            names.append(platform)
    if not names:
        return "Any channel"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"
