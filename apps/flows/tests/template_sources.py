"""The shipped templates, as Python definitions the real exporter turns into JSON.

``flow-templates/*.json`` is checked byte-for-byte against
``serialize(export_document(imported_flow))`` by
:mod:`apps.flows.tests.test_portability_library`. That is a strong guarantee and
an unforgiving one: a hand-written file has to independently rediscover every
convention of :mod:`apps.flows.portability.export` — manifest entries in
first-appearance walk order, an entry keyed by its ref when something addresses
it by id and by its case-folded name otherwise, the ``detail`` a custom field
carries, ``folder`` emitted even when empty, a comment trigger's ``post_ids``
emptied rather than removed. Across a library this size that is a few hundred
hand-maintained manifest rows, each of which must agree with a machine's walk
order.

So the graph and the triggers are written here, where they are readable and
reviewable, and **the envelope is generated**. The exporter is the only thing
that gets the mechanical part right by construction, and using it means the
library cannot drift from the format the importer accepts.

Regenerate with::

    BRIGHTBEAN_REGENERATE_TEMPLATES=1 pytest apps/flows/tests/test_template_library_sources.py

which makes ``flow-templates/*.json`` a build artifact with a staleness gate, the
same arrangement ``static/flows/flow-schema.json`` has with
``export_flow_schema --check``.

**Not** a management command: this needs a database and a throwaway workspace, so
a command shipped to every self-hoster would write rows into *their* database and
files into a source tree they do not have. ``validate_flow_templates`` already
covers the contributor-facing need.
"""

from dataclasses import dataclass, field
from typing import Any

from apps.flows.models import Flow

__all__ = ["DEFINITIONS", "TemplateSource", "build", "definition_for"]


@dataclass(frozen=True)
class TriggerSource:
    """One trigger, as the template means it rather than as a row.

    ``platform`` and not a connection: SPEC §5 makes a null connection mean "all
    connections of a matching platform", and the platform is the half that
    travels. :func:`build` makes whatever connection the platform needs.
    """

    type: str
    platform: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateSource:
    """One shipped template, before the exporter has seen it."""

    filename: str
    name: str
    folder: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = field(default_factory=list)
    triggers: list[TriggerSource] = field(default_factory=list)


def build(tenancy: Any, source: TemplateSource) -> Flow:
    """``source`` as a real draft flow with real triggers, in ``tenancy``.

    Through the services the product uses rather than ``objects.create``: a
    definition that only builds by going around the validation the app applies
    is a definition that can describe a flow the app would refuse.
    """
    from apps.flows.services import create_flow, save_draft
    from apps.flows.tests.support import connection_for, graph
    from apps.flows.triggers.services import create_trigger

    workspace = tenancy.workspace
    flow = create_flow(workspace=workspace, name=source.name, folder=source.folder)
    save_draft(flow, graph(list(source.nodes), list(source.edges)))

    connections: dict[str, Any] = {}
    for trigger in source.triggers:
        connection = connections.get(trigger.platform)
        if connection is None:
            connection = connection_for(
                workspace, platform=trigger.platform, external_id=f"{trigger.platform}-template"
            )
            connections[trigger.platform] = connection
        create_trigger(flow, trigger_type=trigger.type, config=dict(trigger.config), connection=connection)
    return flow


def definition_for(filename: str) -> TemplateSource | None:
    """The definition behind a shipped filename, or ``None`` if nothing owns it."""
    return next((source for source in DEFINITIONS if source.filename == filename), None)


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------


def _node(node_id: str, node_type: str, config: dict[str, Any] | None = None, *, x: int = 0) -> dict[str, Any]:
    from apps.flows.tests.support import node

    return node(node_id, node_type, config, x=x)


def _edge(source: str, handle: str, target: str) -> dict[str, Any]:
    from apps.flows.tests.support import edge

    return edge(source, handle, target)


def _text(*paragraphs: str) -> dict[str, Any]:
    """A ``send_message`` config that is nothing but text blocks."""
    return {"blocks": [{"type": "text", "text": paragraph} for paragraph in paragraphs]}


def _note(node_id: str, text: str, x: int) -> dict[str, Any]:
    """The "replace these before you publish" card every template carries.

    Notes are ``annotation=True``: no routing, no entry-node effect, no
    requirement. They are the only honest way to ship a template full of
    placeholder copy — the alternative is a flow that looks finished and quietly
    points at ``example.com``.
    """
    from apps.flows.tests.support import node

    return node(node_id, "note", {"text": text}, x=x)


# ---------------------------------------------------------------------------
# The definitions
# ---------------------------------------------------------------------------

#: The template that already shipped, written here first and on purpose. If the
#: generator reproduces this file byte for byte, the loop is correct; the
#: alternative is discovering it is not across nineteen files at once.
_COMMENT_TO_DM_LEAD_MAGNET = TemplateSource(
    filename="instagram-comment-to-dm-lead-magnet.json",
    name="Comment-to-DM lead magnet",
    folder="Starters",
    nodes=[
        _node(
            "deliver",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Thanks for commenting! Here is the guide I promised."},
                    {
                        "type": "card",
                        "title": "The starter guide",
                        "subtitle": "Everything in one PDF.",
                        "url_button": {"label": "Open the guide", "url": "https://example.com/guide"},
                    },
                ],
                "quick_replies": [{"id": "more", "label": "Send me more like this"}],
            },
            x=0,
        ),
        _node(
            "ask_email",
            "data_collection",
            {
                "question": "Where should I send the next one? Reply with your email address.",
                "reply_type": "email",
                "target": {"type": "system_field", "key": "email"},
                "retry": {"max": 2, "invalid_text": "That does not look like an email address - try again?"},
                "timeout": {"enabled": True, "delay": 2, "unit": "days"},
            },
            x=300,
        ),
        _node("tag_lead", "action", {"actions": [{"verb": "add_tag", "tag": "Lead magnet"}]}, x=600),
        _node("thanks", "send_message", _text("You are on the list. Talk soon!"), x=900),
    ],
    edges=[
        _edge("deliver", "qr:more", "ask_email"),
        _edge("ask_email", "default", "tag_lead"),
        _edge("tag_lead", "default", "thanks"),
    ],
    triggers=[
        TriggerSource(
            type="comment",
            platform="instagram",
            config={
                "post_scope": "all",
                "include_keywords": ["guide", "send me"],
                "top_level_only": True,
                "public_reply": {"mode": "random", "texts": ["Just sent it over!", "Check your DMs."]},
                "like_comment": True,
                "once_per_contact_per_post": True,
            },
        )
    ],
)


def _comment(*keywords: str, replies: tuple[str, ...] = (), like: bool = True) -> TriggerSource:
    """A comment trigger on every post, narrowed by keyword.

    ``post_scope: "all"`` and not ``"specific"``: a specific scope exports with
    its ``post_ids`` emptied (the ids are the exporter's own posts), so a
    template shipped that way matches nothing until somebody lists their posts.
    Narrowing to particular posts is a thing the importer does afterwards, and
    the note in each template says so.
    """
    config: dict[str, Any] = {
        "post_scope": "all",
        "include_keywords": list(keywords),
        "top_level_only": True,
        "like_comment": like,
        "once_per_contact_per_post": True,
    }
    if replies:
        config["public_reply"] = {"mode": "random", "texts": list(replies)}
    return TriggerSource(type="comment", platform="instagram", config=config)


def _keyword(*words: str, mode: str = "exact") -> TriggerSource:
    """A keyword trigger. Uppercase single tokens read as instructions in a caption."""
    return TriggerSource(
        type="keyword",
        platform="instagram",
        config={"keywords": [{"text": word, "mode": mode} for word in words]},
    )


def _card(title: str, subtitle: str, label: str, url: str) -> dict[str, Any]:
    """A standalone card block. Note the ``type`` key a gallery card must not have."""
    return {
        "type": "card",
        "title": title,
        "subtitle": subtitle,
        "url_button": {"label": label, "url": url},
    }


def _gallery_card(title: str, subtitle: str, label: str, url: str) -> dict[str, Any]:
    """One card inside a gallery.

    ``gallery_card`` has **no** ``type`` key while ``block_card`` requires one,
    and every object in this schema is closed — so copying a standalone card into
    a gallery is rejected, and the reverse loses the discriminator.

    No ``image``: that one field holds "media library id **or** URL", and
    ``refs.is_uuid`` decides which. A UUID there becomes a ``media`` requirement,
    which is not creatable on import, so the template would stop importing into an
    empty workspace. A template cannot ship artwork; the note says to add it.
    """
    return {"title": title, "subtitle": subtitle, "url_button": {"label": label, "url": url}}


_REPLACE = "Before you publish: swap every example.com link for your own, rename the tags to match yours, and rewrite the copy in your voice."


# ---------------------------------------------------------------------------
# Comment-triggered
# ---------------------------------------------------------------------------

_FOLLOW_TO_UNLOCK = TemplateSource(
    filename="instagram-comment-follow-to-unlock.json",
    name="Follow to unlock",
    folder="Grow",
    nodes=[
        _node(
            "ask",
            "send_message",
            {
                "blocks": [
                    {
                        "type": "text",
                        "text": (
                            "Hey {{first_name}}! Happy to send this over, and it goes out to followers first. "
                            "Give the account a follow and tap below."
                        ),
                    }
                ],
                "quick_replies": [
                    {"id": "followed", "label": "Done, I followed"},
                    {"id": "later", "label": "Maybe later"},
                ],
            },
            x=0,
        ),
        _node(
            "deliver",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Amazing, here it is. Enjoy!"},
                    _card("The thing you asked for", "Yours to keep.", "Open it", "https://example.com/unlock"),
                ]
            },
            x=300,
        ),
        _node("tag_follower", "action", {"actions": [{"verb": "add_tag", "tag": "Follow unlock"}]}, x=600),
        _node(
            "later",
            "send_message",
            _text("No problem. The link stays here whenever you want it."),
            x=300,
        ),
        _note(
            "why_honour_system",
            "This asks people to confirm the follow rather than checking it. Instagram with Instagram "
            "Login publishes no follow webhook, so nothing can verify it \u2014 there is no 'is a follower' "
            "condition to branch on. The tag is what you segment on later.\n\n" + _REPLACE,
            900,
        ),
    ],
    edges=[
        _edge("ask", "qr:followed", "deliver"),
        _edge("ask", "qr:later", "later"),
        _edge("deliver", "default", "tag_follower"),
    ],
    triggers=[_comment("unlock", "want it", "send it", replies=("Check your DMs!", "Just sent it over."))],
)


_LINK_IN_DM = TemplateSource(
    filename="instagram-comment-link-in-dm.json",
    name="Auto-DM the link from comments",
    folder="Convert",
    nodes=[
        _node(
            "opener",
            "send_message",
            {
                "blocks": [
                    {
                        "type": "text",
                        "text": (
                            "Hi {{first_name}}, thanks for commenting! Tap below and I will send the link "
                            "straight over."
                        ),
                    }
                ],
                "buttons": [{"id": "send", "label": "Send me the link", "action": "postback"}],
            },
            x=0,
        ),
        _node(
            "link",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Here you go, straight to the page."},
                    _card(
                        "What you asked about",
                        "Everything is on this page.",
                        "Open the link",
                        "https://example.com/link",
                    ),
                ]
            },
            x=300,
        ),
        _note(
            "opening_dm",
            "The first message exists to open the 24-hour messaging window \u2014 Instagram only lets you "
            "reply inside it, and a tap is what starts the clock. Sending the link straight away works too, "
            "but you lose the chance to follow up.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("opener", "btn:send", "link")],
    triggers=[_comment("link", "send", "info", replies=("Sent!", "Check your DMs, it is there."))],
)


_AFFILIATE_PICKS = TemplateSource(
    filename="instagram-comment-affiliate-picks.json",
    name="Affiliate picks from comments",
    folder="Convert",
    nodes=[
        _node(
            "picks",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Here are the three I actually use. Swipe through."},
                    {
                        "type": "gallery",
                        "cards": [
                            _gallery_card(
                                "First pick", "The one I reach for daily.", "See it", "https://example.com/pick-one"
                            ),
                            _gallery_card(
                                "Second pick", "Best value of the three.", "See it", "https://example.com/pick-two"
                            ),
                            _gallery_card(
                                "Third pick",
                                "Worth it if you use it often.",
                                "See it",
                                "https://example.com/pick-three",
                            ),
                        ],
                    },
                ]
            },
            x=0,
        ),
        _node("tag_shopper", "action", {"actions": [{"verb": "add_tag", "tag": "Affiliate interest"}]}, x=300),
        _note(
            "disclosure",
            "Affiliate links usually have to be disclosed. Say so in the first message \u2014 the rules "
            "differ by country and by network, and the DM is where people read it.\n\n"
            "A gallery holds up to ten cards. Add your own photos in the builder: a template cannot ship "
            "images, so every card here is text only until you pick one.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("picks", "default", "tag_shopper")],
    triggers=[_comment("links", "shop", "where", replies=("Sent the links!", "Check your DMs."))],
)


_PRODUCT_GALLERY = TemplateSource(
    filename="instagram-comment-product-gallery.json",
    name="Product lineup in DMs",
    folder="Convert",
    nodes=[
        _node(
            "lineup",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Here is the full lineup. Anything you want to know?"},
                    {
                        "type": "gallery",
                        "cards": [
                            _gallery_card(
                                "The everyday one", "Our bestseller.", "View", "https://example.com/everyday"
                            ),
                            _gallery_card("The upgrade", "More of everything.", "View", "https://example.com/upgrade"),
                            _gallery_card("The bundle", "Both, for less.", "View", "https://example.com/bundle"),
                        ],
                    },
                ],
                "quick_replies": [
                    {"id": "sizing", "label": "How do sizes run?"},
                    {"id": "shipping", "label": "Shipping and returns"},
                ],
            },
            x=0,
        ),
        _node("sizing", "send_message", _text("They run true to size. If you are between two, size up."), x=300),
        _node(
            "shipping",
            "send_message",
            _text("Free shipping over the threshold, and 30 days to change your mind."),
            x=300,
        ),
        _note(
            "two_questions",
            "The two quick replies are the questions that actually stop a sale. Replace them with yours "
            "\u2014 read your own DMs for a week and use the two you answer most.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[
        _edge("lineup", "qr:sizing", "sizing"),
        _edge("lineup", "qr:shipping", "shipping"),
    ],
    triggers=[_comment("lineup", "shop", "products", replies=("Sent the lineup!", "Check your DMs."))],
)


_REEL_TO_PRODUCT = TemplateSource(
    filename="instagram-comment-reel-to-product.json",
    name="Sell from Reel comments",
    folder="Convert",
    nodes=[
        _node(
            "pitch",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "That is the one from the Reel. Here it is."},
                    _card("The thing from the Reel", "In stock now.", "Buy it", "https://example.com/reel-product"),
                ]
            },
            x=0,
        ),
        _node("tag_interest", "action", {"actions": [{"verb": "add_tag", "tag": "Reel interest"}]}, x=300),
        _note(
            "same_field",
            "Reel comments arrive on the same Instagram comments field as post comments, so this is an "
            "ordinary comment trigger \u2014 there is no separate Reel trigger. If you only want it on "
            "Reels, narrow the trigger to specific posts once the Reel is live.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("pitch", "default", "tag_interest")],
    triggers=[_comment("price", "how much", "link", replies=("Just sent it!", "Check your DMs."))],
)


_RSVP = TemplateSource(
    filename="instagram-comment-rsvp.json",
    name="Comments into RSVPs",
    folder="Convert",
    nodes=[
        _node(
            "confirm",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "You are in, {{first_name}}. Here are the details."},
                    _card("Save your spot", "Add it to your calendar.", "Add to calendar", "https://example.com/rsvp"),
                ]
            },
            x=0,
        ),
        _node("tag_rsvp", "action", {"actions": [{"verb": "add_tag", "tag": "RSVP yes"}]}, x=300),
        _note(
            "reminders",
            "The tag is the point: broadcast the reminder to everyone carrying 'RSVP yes' the day before. "
            "A per-person reminder cannot live in this flow \u2014 Instagram closes the messaging window "
            "24 hours after their last message.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("confirm", "default", "tag_rsvp")],
    triggers=[_comment("in", "join", "rsvp", replies=("You are in!", "Saved you a spot. Check your DMs."))],
)


# ---------------------------------------------------------------------------
# Default reply
# ---------------------------------------------------------------------------

_AUTORESPONDER = TemplateSource(
    filename="instagram-default-reply-autoresponder.json",
    name="Respond to every DM",
    folder="Engage",
    nodes=[
        _node(
            "reply",
            "send_message",
            {
                "blocks": [
                    {
                        "type": "text",
                        "text": (
                            "Hi {{first_name}}, thanks for the message! I read everything here. "
                            "Pick one below and I can help right away."
                        ),
                    }
                ],
                "quick_replies": [
                    {"id": "shop", "label": "I want to buy"},
                    {"id": "help", "label": "I have a question"},
                    {"id": "human", "label": "Talk to a person"},
                ],
            },
            x=0,
        ),
        _node(
            "shop",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Everything is here. Shout if you want a recommendation."},
                    _card("Shop", "The full range.", "Browse", "https://example.com/shop"),
                ]
            },
            x=300,
        ),
        _node("help", "send_message", _text("Ask away and I will get you an answer."), x=300),
        _node("human", "action", {"actions": [{"verb": "open_conversation"}]}, x=300),
        _node("handoff", "send_message", _text("A person is picking this up now, hang tight."), x=600),
        _note(
            "personalised_not_ai",
            "'Personalised' here means {{placeholders}}, not AI: the greeting fills in whatever you know "
            "about the contact. There is no intent detection in this flow.\n\n"
            "A default reply runs only when nothing else matched, and at most once per contact per day \u2014 "
            "so keyword automations always win, and nobody gets this twice.\n\n" + _REPLACE,
            900,
        ),
    ],
    edges=[
        _edge("reply", "qr:shop", "shop"),
        _edge("reply", "qr:help", "help"),
        _edge("reply", "qr:human", "human"),
        _edge("human", "default", "handoff"),
    ],
    triggers=[TriggerSource(type="default_reply", platform="instagram", config={})],
)


# ---------------------------------------------------------------------------
# Keyword-triggered
# ---------------------------------------------------------------------------

_LINK_DROP = TemplateSource(
    filename="instagram-keyword-link-drop.json",
    name="Drop the link in DMs",
    folder="Engage",
    nodes=[
        _node(
            "link",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Here it is, {{first_name}}. No more hunting through the bio."},
                    _card("The page you wanted", "Everything lives here.", "Open it", "https://example.com/link"),
                ]
            },
            x=0,
        ),
        _note(
            "one_word",
            "Put the keyword in the caption and the Story: 'DM me LINK'. One word people can type without "
            "thinking beats a sentence they have to remember.\n\n" + _REPLACE,
            300,
        ),
    ],
    triggers=[_keyword("LINK", "SITE")],
)


_YOUTUBE = TemplateSource(
    filename="instagram-keyword-youtube-subscribe.json",
    name="Send people to YouTube",
    folder="Grow",
    nodes=[
        _node(
            "send",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "The full version is on YouTube. Here you go."},
                    _card("Watch the long version", "New one every week.", "Watch now", "https://example.com/youtube"),
                ]
            },
            x=0,
        ),
        _node("tag_viewer", "action", {"actions": [{"verb": "add_tag", "tag": "YouTube viewer"}]}, x=300),
        _note(
            "own_url",
            "Replace the placeholder with your own channel URL. A template cannot ship a real link to "
            "somebody else's channel.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("send", "default", "tag_viewer")],
    triggers=[_keyword("VIDEO", "WATCH", "YT")],
)


_WHERE_IS_THIS_FROM = TemplateSource(
    filename="instagram-keyword-where-is-this-from.json",
    name="Answer 'where is this from?'",
    folder="Engage",
    nodes=[
        _node(
            "answer",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Good eye. Here is the exact one."},
                    _card("The one you spotted", "Same colour, same size.", "See it", "https://example.com/that-one"),
                ]
            },
            x=0,
        ),
        _node("tag_question", "action", {"actions": [{"verb": "add_tag", "tag": "Product question"}]}, x=300),
        _note(
            "not_live_comments",
            "ManyChat runs its version of this on Live comments. There is no live-comment trigger here, so "
            "this listens for the words in a DM instead. Say the keyword out loud during the Live and it "
            "does the same job.\n\n"
            "'where' matches as a whole word anywhere in the message, so 'where is this from?' and 'where "
            "did you get that' both land.\n\n" + _REPLACE,
            600,
        ),
    ],
    edges=[_edge("answer", "default", "tag_question")],
    triggers=[_keyword("where", "link", mode="any_word")],
)


_COURSE_EARLY_ACCESS = TemplateSource(
    filename="instagram-keyword-course-early-access.json",
    name="Early access to the launch",
    folder="Convert",
    nodes=[
        _node(
            "pitch",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "It opens Thursday, and this list gets in first."},
                    _card(
                        "Early access", "Before anyone else.", "See what is inside", "https://example.com/early-access"
                    ),
                ],
                "quick_replies": [{"id": "notify", "label": "Put me on the list"}],
            },
            x=0,
        ),
        _node("tag_waitlist", "action", {"actions": [{"verb": "add_tag", "tag": "Waitlist"}]}, x=300),
        _node(
            "confirm",
            "send_message",
            _text("You are on it. I will message you the moment doors open."),
            x=600,
        ),
        _note(
            "launch_day",
            "On launch day, broadcast to everyone carrying 'Waitlist'. This flow cannot message them later "
            "by itself: Instagram closes the 24-hour window after their last message, so the promise is kept "
            "by the broadcast, not by this graph.\n\n" + _REPLACE,
            900,
        ),
    ],
    edges=[
        _edge("pitch", "qr:notify", "tag_waitlist"),
        _edge("tag_waitlist", "default", "confirm"),
    ],
    triggers=[_keyword("EARLY", "ACCESS", "COURSE")],
)


_EMAIL_CAPTURE = TemplateSource(
    filename="instagram-keyword-email-capture.json",
    name="Grow the email list",
    folder="Grow",
    nodes=[
        _node(
            "offer",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Happy to send it. Where should it go?"},
                    {"type": "text", "text": "One email a week, unsubscribe whenever. Nothing else."},
                ],
                "quick_replies": [
                    {"id": "yes", "label": "Send it over"},
                    {"id": "no", "label": "Not right now"},
                ],
            },
            x=0,
        ),
        _node(
            "ask_email",
            "data_collection",
            {
                "question": "Perfect. What is your email address?",
                "reply_type": "email",
                "target": {"type": "system_field", "key": "email"},
                "retry": {"max": 2, "invalid_text": "That does not look like an email address. Try once more?"},
                "timeout": {"enabled": True, "delay": 6, "unit": "hours"},
            },
            x=300,
        ),
        _node("tag_subscriber", "action", {"actions": [{"verb": "add_tag", "tag": "Email list"}]}, x=600),
        _node(
            "deliver",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Sent. It should land in a minute."},
                    _card("Your download", "Grab it here too.", "Open it", "https://example.com/download"),
                ]
            },
            x=900,
        ),
        _node("declined", "send_message", _text("All good. The offer stands whenever you want it."), x=300),
        _note(
            "consent",
            "The email answer records consent along with the address, which is the record you want if "
            "anybody ever asks how a contact joined the list. Keep the sentence about what you send and how "
            "to leave: that is the part the consent record refers to.\n\n"
            "This captures the address; it does not send email. Push the list to your email tool and send "
            "from there.\n\n" + _REPLACE,
            1200,
        ),
    ],
    edges=[
        _edge("offer", "qr:yes", "ask_email"),
        _edge("offer", "qr:no", "declined"),
        _edge("ask_email", "default", "tag_subscriber"),
        _edge("tag_subscriber", "default", "deliver"),
    ],
    triggers=[_keyword("GUIDE", "FREEBIE", "EBOOK")],
)


_SMS_LIST = TemplateSource(
    filename="instagram-keyword-sms-list.json",
    name="Grow the SMS list",
    folder="Grow",
    nodes=[
        _node(
            "offer",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Drops go out by text first. Want in?"},
                    {"type": "text", "text": "A couple of texts a month. Reply STOP any time and they end."},
                ],
                "quick_replies": [
                    {"id": "join", "label": "Text me the drops"},
                    {"id": "skip", "label": "No thanks"},
                ],
            },
            x=0,
        ),
        _node(
            "ask_phone",
            "data_collection",
            {
                "question": "What is the best number for you? Include the country code.",
                "reply_type": "phone",
                "target": {"type": "system_field", "key": "phone"},
                "retry": {"max": 2, "invalid_text": "That number did not parse. Try it with the country code?"},
                "timeout": {"enabled": True, "delay": 6, "unit": "hours"},
            },
            x=300,
        ),
        _node("tag_sms", "action", {"actions": [{"verb": "add_tag", "tag": "SMS subscriber"}]}, x=600),
        _node("confirm", "send_message", _text("You are on the list. First text comes with the next drop."), x=900),
        _node("skip", "send_message", _text("No problem. Everything still goes out here too."), x=300),
        _note(
            "consent_and_stop",
            "The phone answer records consent with the number, and the SMS channel answers STOP and HELP on "
            "its own. Keep the sentence saying how often you text and how to stop: in most countries that "
            "sentence is the legal part, not a nicety.\n\n"
            "This collects numbers. It does not text anybody. Sending is a broadcast to the tag, or the "
            "'Move the conversation to SMS' template.\n\n" + _REPLACE,
            1200,
        ),
    ],
    edges=[
        _edge("offer", "qr:join", "ask_phone"),
        _edge("offer", "qr:skip", "skip"),
        _edge("ask_phone", "default", "tag_sms"),
        _edge("tag_sms", "default", "confirm"),
    ],
    triggers=[_keyword("TEXTME", "DROPS", "SMS")],
)


_DM_TO_SMS = TemplateSource(
    filename="instagram-keyword-dm-to-sms.json",
    name="Move the conversation to SMS",
    folder="Grow",
    nodes=[
        _node(
            "offer",
            "send_message",
            {
                "blocks": [
                    {
                        "type": "text",
                        "text": (
                            "Instagram closes this chat 24 hours after your last message. Text is easier. "
                            "Want me to move us over?"
                        ),
                    }
                ],
                "quick_replies": [
                    {"id": "yes", "label": "Yes, text me"},
                    {"id": "no", "label": "Stay here"},
                ],
            },
            x=0,
        ),
        _node(
            "ask_phone",
            "data_collection",
            {
                "question": "What number should I use? Include the country code.",
                "reply_type": "phone",
                "target": {"type": "system_field", "key": "phone"},
                "retry": {"max": 2, "invalid_text": "That number did not parse. Try it with the country code?"},
                "timeout": {"enabled": True, "delay": 6, "unit": "hours"},
            },
            x=300,
        ),
        _node(
            "first_text",
            "send_sms",
            {"text": "It is me, from Instagram. Reply here and I will pick it up. Reply STOP to end these."},
            x=600,
        ),
        _node("tag_moved", "action", {"actions": [{"verb": "add_tag", "tag": "Moved to SMS"}]}, x=900),
        _node(
            "sms_failed",
            "send_message",
            _text("That did not go through, so let us carry on here instead."),
            x=900,
        ),
        _node("stay", "send_message", _text("Staying right here then. What did you want to know?"), x=300),
        _note(
            "two_channels",
            "This flow needs two connections: Instagram for the trigger and SMS for the text. Only the "
            "Instagram one is asked for at import, because the requirements list is built from triggers and "
            "nothing here triggers on SMS. Connect SMS in Settings before you publish, or the Send SMS step "
            "takes its error path every time.\n\n"
            "That error path is wired on purpose: no SMS connection and no phone number both land there, so "
            "the conversation carries on in the DM instead of stopping dead.\n\n" + _REPLACE,
            1200,
        ),
    ],
    edges=[
        _edge("offer", "qr:yes", "ask_phone"),
        _edge("offer", "qr:no", "stay"),
        _edge("ask_phone", "default", "first_text"),
        _edge("first_text", "default", "tag_moved"),
        _edge("first_text", "error", "sms_failed"),
    ],
    triggers=[_keyword("TEXT", "SMS")],
)


_FAQ_HUB = TemplateSource(
    filename="instagram-keyword-faq-hub.json",
    name="Answer the usual questions",
    folder="Engage",
    nodes=[
        _node(
            "hub",
            "send_message",
            {
                "blocks": [{"type": "text", "text": "Hi {{first_name}}! Here are the things people ask most."}],
                "quick_replies": [
                    {"id": "pricing", "label": "What does it cost?"},
                    {"id": "shipping", "label": "Shipping"},
                    {"id": "returns", "label": "Returns"},
                    {"id": "human", "label": "Something else"},
                ],
            },
            x=0,
        ),
        _node(
            "pricing",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Plans start at $19 a month, and the annual one saves two months."},
                    _card("Pricing", "Every plan side by side.", "Compare plans", "https://example.com/pricing"),
                ]
            },
            x=300,
        ),
        _node(
            "shipping",
            "send_message",
            _text("Orders leave within two working days, and you get a tracking link the moment they do."),
            x=300,
        ),
        _node(
            "returns",
            "send_message",
            _text("Thirty days, no questions. Message here and I will send a returns label."),
            x=300,
        ),
        _node("human", "action", {"actions": [{"verb": "open_conversation"}]}, x=300),
        _node("handoff", "send_message", _text("Passing this to a person now. You will hear back shortly."), x=600),
        _note(
            "one_trigger",
            "One trigger, one hub, four answers. A trigger cannot pick which node a flow starts at, so "
            "'one keyword per question' would mean one flow per question. The hub is the shape that works.\n\n"
            "Matching is literal: exact, contains, or whole-word. There is no fuzzy or semantic matching, so "
            "add the spellings people actually type rather than hoping for the best.\n\n"
            "Four quick replies because four fit on a phone. Instagram allows thirteen; nobody reads "
            "thirteen.\n\n" + _REPLACE,
            900,
        ),
    ],
    edges=[
        _edge("hub", "qr:pricing", "pricing"),
        _edge("hub", "qr:shipping", "shipping"),
        _edge("hub", "qr:returns", "returns"),
        _edge("hub", "qr:human", "human"),
        _edge("human", "default", "handoff"),
    ],
    triggers=[_keyword("FAQ", "HELP", "QUESTION", "SUPPORT")],
)


_QUALIFY_QUIZ = TemplateSource(
    filename="instagram-keyword-qualify-quiz.json",
    name="Qualify with a quiz",
    folder="Engage",
    nodes=[
        _node(
            "intro",
            "send_message",
            {
                "blocks": [{"type": "text", "text": "Two questions and I will point you at the right thing."}],
                "quick_replies": [{"id": "start", "label": "Go on then"}],
            },
            x=0,
        ),
        _node(
            "stage",
            "send_message",
            {
                "blocks": [{"type": "text", "text": "Where are you right now?"}],
                "quick_replies": [
                    {"id": "starting", "label": "Just starting"},
                    {"id": "growing", "label": "Growing"},
                    {"id": "established", "label": "Established"},
                ],
            },
            x=300,
        ),
        _node("tag_starting", "action", {"actions": [{"verb": "add_tag", "tag": "Quiz: starting out"}]}, x=600),
        _node("tag_growing", "action", {"actions": [{"verb": "add_tag", "tag": "Quiz: growing"}]}, x=600),
        _node("tag_established", "action", {"actions": [{"verb": "add_tag", "tag": "Quiz: established"}]}, x=600),
        _node(
            "offer_starting",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Start here. It is the shortest path to a first result."},
                    _card("The starter", "Built for day one.", "Take a look", "https://example.com/starter"),
                ]
            },
            x=900,
        ),
        _node(
            "offer_growing",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "You are past the basics. This is the one that scales with you."},
                    _card("The growth plan", "For when it is working.", "Take a look", "https://example.com/growth"),
                ]
            },
            x=900,
        ),
        _node(
            "offer_established",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "At your size this is usually a conversation rather than a page."},
                    _card("Talk to us", "Tell us what you need.", "Book a call", "https://example.com/call"),
                ]
            },
            x=900,
        ),
        _node(
            "ask_email",
            "data_collection",
            {
                "question": "Want the full breakdown by email? Drop your address and I will send it.",
                "reply_type": "email",
                "target": {"type": "system_field", "key": "email"},
                "retry": {"max": 2, "invalid_text": "That does not look like an email address. Try once more?"},
                "timeout": {"enabled": True, "delay": 6, "unit": "hours"},
            },
            x=1200,
        ),
        _node(
            "has_email",
            "condition",
            {"match": "all", "rules": [{"source": "system_field", "key": "email", "op": "has_value"}]},
            x=1500,
        ),
        _node("thanks", "send_message", _text("On its way. Thanks for playing along."), x=1800),
        _node("no_email", "send_message", _text("No bother. Everything above still stands."), x=1800),
        _note(
            "tags_are_the_point",
            "The three tags are what this is for. Afterwards you can broadcast to one group without "
            "touching the other two, which is the whole reason to ask.\n\n"
            "The condition reads a contact field, not a tag. Contact-field rules cost nothing at import "
            "time, while a tag rule has to be matched or created first. Either works; this one is the "
            "cheaper default.\n\n" + _REPLACE,
            2100,
        ),
    ],
    edges=[
        _edge("intro", "qr:start", "stage"),
        _edge("stage", "qr:starting", "tag_starting"),
        _edge("stage", "qr:growing", "tag_growing"),
        _edge("stage", "qr:established", "tag_established"),
        _edge("tag_starting", "default", "offer_starting"),
        _edge("tag_growing", "default", "offer_growing"),
        _edge("tag_established", "default", "offer_established"),
        _edge("offer_starting", "default", "ask_email"),
        _edge("offer_growing", "default", "ask_email"),
        _edge("offer_established", "default", "ask_email"),
        _edge("ask_email", "default", "has_email"),
        _edge("ask_email", "timeout", "no_email"),
        _edge("has_email", "cond:true", "thanks"),
        _edge("has_email", "cond:false", "no_email"),
    ],
    triggers=[_keyword("QUIZ", "HELPME")],
)


# ---------------------------------------------------------------------------
# Story-reply triggered
# ---------------------------------------------------------------------------

_COLLAB_REQUESTS = TemplateSource(
    filename="instagram-story-collab-requests.json",
    name="Sort collab requests from Stories",
    folder="Grow",
    nodes=[
        _node(
            "intro",
            "send_message",
            {
                "blocks": [{"type": "text", "text": "Thanks for replying! Quick one so I send this the right way."}],
                "quick_replies": [
                    {"id": "brand", "label": "I am a brand"},
                    {"id": "creator", "label": "I am a creator"},
                ],
            },
            x=0,
        ),
        _node(
            "ask_details",
            "data_collection",
            {
                "question": "Tell me the brand and roughly what you have in mind. A sentence is plenty.",
                "reply_type": "text",
                "target": {"type": "custom_field", "key": "Collab brief"},
                "retry": {"max": 1, "invalid_text": "Did not catch that. One more go?"},
                "timeout": {"enabled": True, "delay": 12, "unit": "hours"},
            },
            x=300,
        ),
        _node("tag_collab", "action", {"actions": [{"verb": "add_tag", "tag": "Collab request"}]}, x=600),
        _node(
            "wrap",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Got it. I go through these on Fridays and will come back to you."},
                    _card(
                        "Rates and audience",
                        "Everything in one place.",
                        "See the media kit",
                        "https://example.com/media-kit",
                    ),
                ]
            },
            x=900,
        ),
        _node(
            "creator_reply",
            "send_message",
            _text("Always up for a creator collab. Tell me what you are thinking and I will reply here."),
            x=300,
        ),
        _note(
            "one_field",
            "The brief lands in a contact field, so it is on the contact record when you open the "
            "conversation rather than buried in the thread.\n\n"
            "Story replies only count while the Story is live. Once it expires the trigger has nothing to "
            "fire on, so post the prompt on a schedule you can keep.\n\n" + _REPLACE,
            1200,
        ),
    ],
    edges=[
        _edge("intro", "qr:brand", "ask_details"),
        _edge("intro", "qr:creator", "creator_reply"),
        _edge("ask_details", "default", "tag_collab"),
        _edge("tag_collab", "default", "wrap"),
    ],
    triggers=[
        TriggerSource(
            type="story_reply",
            platform="instagram",
            config={
                "keywords": [
                    {"text": "collab", "mode": "any_word"},
                    {"text": "partner", "mode": "any_word"},
                    {"text": "work together", "mode": "contains"},
                ]
            },
        )
    ],
)


_LIMITED_TIME_OFFER = TemplateSource(
    filename="instagram-story-limited-time-offer.json",
    name="Limited-time offer from Stories",
    folder="Convert",
    nodes=[
        _node(
            "offer",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Here is the code from the Story. It is good for today only."},
                    _card("Your code", "Applies at checkout.", "Use it now", "https://example.com/offer"),
                ]
            },
            x=0,
        ),
        _node("tag_offer", "action", {"actions": [{"verb": "add_tag", "tag": "Story offer"}]}, x=300),
        _node("wait", "smart_delay", {"mode": "duration", "duration": {"value": 20, "unit": "hours"}}, x=600),
        _node(
            "nudge",
            "send_message",
            {
                "blocks": [
                    {"type": "text", "text": "Last call on that code, {{first_name}}. It stops working tonight."},
                    _card("Still yours", "Until midnight.", "Use it now", "https://example.com/offer"),
                ]
            },
            x=900,
        ),
        _note(
            "twenty_hours",
            "Twenty hours, not twenty-four. Instagram closes the messaging window 24 hours after the "
            "contact's last message, and a reminder scheduled at the edge of it arrives to a closed door.\n\n"
            "The deadline lives in the copy rather than in the delay. A fixed date baked into a template is "
            "already in the past by the time somebody downloads it, so the flow counts hours from the reply "
            "instead.\n\n" + _REPLACE,
            1200,
        ),
    ],
    edges=[
        _edge("offer", "default", "tag_offer"),
        _edge("tag_offer", "default", "wait"),
        _edge("wait", "default", "nudge"),
    ],
    triggers=[
        TriggerSource(
            type="story_reply",
            platform="instagram",
            config={
                "keywords": [
                    {"text": "offer", "mode": "any_word"},
                    {"text": "code", "mode": "any_word"},
                    {"text": "deal", "mode": "any_word"},
                ]
            },
        )
    ],
)


DEFINITIONS: tuple[TemplateSource, ...] = (
    _AFFILIATE_PICKS,
    _FOLLOW_TO_UNLOCK,
    _LINK_IN_DM,
    _PRODUCT_GALLERY,
    _REEL_TO_PRODUCT,
    _RSVP,
    _COMMENT_TO_DM_LEAD_MAGNET,
    _AUTORESPONDER,
    _COURSE_EARLY_ACCESS,
    _DM_TO_SMS,
    _EMAIL_CAPTURE,
    _FAQ_HUB,
    _LINK_DROP,
    _QUALIFY_QUIZ,
    _SMS_LIST,
    _WHERE_IS_THIS_FROM,
    _YOUTUBE,
    _COLLAB_REQUESTS,
    _LIMITED_TIME_OFFER,
)
