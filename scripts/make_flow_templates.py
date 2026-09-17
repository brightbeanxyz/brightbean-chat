"""Author the shipped flow templates, through the real exporter.

``flow-templates/`` is read by the gallery and by the import wizard, and every
file in it carries a ``requirements`` manifest naming the tags, custom fields,
channels and members an importing workspace has to answer for. That manifest is
derived from the graph, and deriving it by hand is how you ship a template whose
wizard asks for the wrong things.

So the templates are **written as graphs here and exported by
``apps.flows.portability.export_document``** — the same code path the Download a
copy button uses. Whatever the exporter would produce for a real flow is what
lands in the directory, which makes a hand-edit of a JSON file the unusual act
rather than the normal one.

Run it with::

    .venv/bin/python scripts/make_flow_templates.py

It writes every template, overwriting what is there. The work happens inside a
transaction that is rolled back, so the database it runs against keeps nothing.

``apps/flows/tests/test_template_library.py`` validates and imports every file,
so a template that stops working is a red build rather than a download that
fails for a stranger.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from apps.flows.models import Flow, FlowStatus, FlowVersion, Trigger  # noqa: E402
from apps.flows.portability import export_document, serialize  # noqa: E402
from apps.organizations.models import Organization  # noqa: E402
from apps.workspaces.models import Workspace  # noqa: E402

FOLDER = "Starters"


# ---------------------------------------------------------------------------
# Small builders, so a template reads as the flow it is rather than as JSON
# ---------------------------------------------------------------------------


def text(*lines: str) -> dict[str, Any]:
    return {"type": "text", "text": "\n".join(lines)}


def send(node_id: str, *blocks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "send_message", "config": {"blocks": list(blocks), **extra}}


def ask(node_id: str, question: str, *, key: str, kind: str = "system_field", reply: str = "text") -> dict[str, Any]:
    """A question whose answer is saved onto the contact."""
    return {
        "id": node_id,
        "type": "data_collection",
        "config": {
            "question": question,
            "reply_type": reply,
            "retry": {"max": 1},
            "target": {"type": kind, "key": key},
            "timeout": {"enabled": True, "delay": 1, "unit": "days"},
        },
    }


def act(node_id: str, *actions: dict[str, Any]) -> dict[str, Any]:
    return {"id": node_id, "type": "action", "config": {"actions": list(actions)}}


def wait(node_id: str, value: int, unit: str = "hours") -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "smart_delay",
        "config": {"mode": "duration", "duration": {"value": value, "unit": unit}},
    }


def note(node_id: str, body: str) -> dict[str, Any]:
    return {"id": node_id, "type": "note", "config": {"text": body}}


def branch(node_id: str, *rules: dict[str, Any], match: str = "all") -> dict[str, Any]:
    return {"id": node_id, "type": "condition", "config": {"match": match, "rules": list(rules)}}


def has_field(key: str) -> dict[str, Any]:
    return {"source": "system_field", "key": key, "op": "has_value"}


def window(platform: str, op: str = "inside") -> dict[str, Any]:
    return {"source": "window", "key": platform, "op": op}


def tag(name: str) -> dict[str, Any]:
    return {"verb": "add_tag", "tag": name}


def field(name: str, value: str) -> dict[str, Any]:
    return {"verb": "set_field", "field": name, "value": value}


def close() -> dict[str, Any]:
    return {"verb": "close_conversation"}


def button(label: str, url: str, ident: str) -> dict[str, Any]:
    return {"id": ident, "label": label, "action": "url", "url": url}


def quick(ident: str, label: str) -> dict[str, Any]:
    return {"id": ident, "label": label}


def chain(*node_ids: str) -> list[tuple[str, str, str]]:
    """The common case: each step runs after the one before it."""
    return [(node_ids[i], "default", node_ids[i + 1]) for i in range(len(node_ids) - 1)]


def keyword(*words: str, mode: str = "any_word") -> dict[str, Any]:
    return {"keywords": [{"text": word, "mode": mode} for word in words]}


def comment_on_any(*words: str) -> dict[str, Any]:
    return {
        "post_scope": "all",
        "post_ids": [],
        "include_keywords": list(words),
        "exclude_keywords": [],
        "top_level_only": True,
        "public_reply": {"mode": "none", "texts": []},
        "like_comment": False,
        "once_per_contact_per_post": True,
    }


# ---------------------------------------------------------------------------
# The templates
# ---------------------------------------------------------------------------
#
# Each entry is (slug, name, platform, trigger list, nodes, edges).
#
# Written to be edited, not run as-is: every one says something specific enough
# to be obviously wrong for somebody else's business, because a template full of
# "Hello! How can I help you today?" gets published unchanged and sounds like it.
# The placeholders are real ({{first_name}}) and the URLs are example.com, which
# is the one domain that cannot accidentally belong to anybody.

TEMPLATES: list[dict[str, Any]] = [
    {
        "slug": "instagram-comment-to-discount-code",
        "name": "Comment for a discount code",
        "triggers": [("comment", "instagram", comment_on_any("code", "CODE", "discount"))],
        "nodes": [
            send(
                "code",
                text("Here you go: SAVE10 takes 10% off your first order.", "It works until the end of the month."),
                buttons=[button("Shop now", "https://example.com/shop", "shop")],
            ),
            act("tag_shopper", tag("Wants a discount")),
            wait("hold", 2, "days"),
            send("nudge", text("Still thinking it over, {{first_name}}? SAVE10 is good until the end of the month.")),
        ],
        "edges": chain("code", "tag_shopper", "hold", "nudge"),
    },
    {
        "slug": "instagram-story-reply-to-conversation",
        "name": "Story reply starts a conversation",
        "triggers": [("story_reply", "instagram", {"keywords": []})],
        "nodes": [
            send(
                "hello",
                text("Thanks for replying to my story!"),
                quick_replies=[quick("buy", "I want to buy"), quick("ask", "I have a question")],
            ),
            act("tag_story", tag("Replied to a story")),
            note(
                "advice",
                "Story replies land in the inbox as normal messages. Add your own answers under each quick reply.",
            ),
        ],
        "edges": chain("hello", "tag_story"),
    },
    {
        "slug": "instagram-story-mention-thank-you",
        "name": "Thank someone for a story mention",
        "triggers": [("story_mention", "instagram", {})],
        "nodes": [
            send("thanks", text("Thank you for the mention, {{first_name}}! That genuinely helps.")),
            act("tag_fan", tag("Mentioned us")),
            wait("hold", 1, "days"),
            send("offer", text("If you ever want to collaborate properly, just reply here.")),
        ],
        "edges": chain("thanks", "tag_fan", "hold", "offer"),
    },
    {
        "slug": "instagram-price-question",
        "name": "Answer price questions on Instagram",
        "triggers": [("comment", "instagram", comment_on_any("price", "cost", "how much"))],
        "nodes": [
            send(
                "answer",
                text("Prices start at £39 and depend on the size you need.", "The full list is on the site:"),
                buttons=[button("See prices", "https://example.com/pricing", "pricing")],
            ),
            ask("ask_which", "Which one were you looking at?", key="Interested in", kind="custom_field"),
            act("tag_warm", tag("Asked about price")),
            send("handover", text("Got it. Someone will come back to you shortly with an exact price.")),
        ],
        "edges": chain("answer", "ask_which", "tag_warm", "handover") + [("ask_which", "timeout", "tag_warm")],
    },
    {
        "slug": "instagram-link-in-bio-capture",
        "name": "Link in bio opens a chat",
        "triggers": [("ref_url", "instagram", {"ref": "bio"})],
        "nodes": [
            send(
                "hello",
                text("Hi! You came from my bio link.", "What are you after today?"),
                quick_replies=[quick("info", "Just looking"), quick("buy", "Ready to buy")],
            ),
            act("tag_bio", tag("Came from bio link"), field("Signup source", "Instagram bio")),
            note(
                "advice", "The reference on this trigger is `bio`. Put the link it gives you in your Instagram profile."
            ),
        ],
        "edges": chain("hello", "tag_bio"),
    },
    {
        "slug": "telegram-welcome-and-faq",
        "name": "Telegram welcome and FAQ",
        "triggers": [
            ("welcome", "telegram", {}),
            ("keyword", "telegram", keyword("help", "faq", "support")),
        ],
        "nodes": [
            send(
                "menu",
                text("Hi {{first_name}}! Thanks for starting a chat. What can I help with?"),
                quick_replies=[
                    quick("pricing", "Pricing"),
                    quick("hours", "Opening hours"),
                    quick("human", "Talk to a person"),
                ],
                retry_unmatched={
                    "enabled": True,
                    "max": 2,
                    "text": "Sorry, I did not catch that. Pick one of the options above.",
                },
            ),
            send(
                "pricing",
                text("Our plans start at $19/month and every plan includes a 14-day trial."),
                buttons=[button("See all plans", "https://example.com/pricing", "plans")],
            ),
            send("hours", text("We answer messages Monday to Friday, 9am-6pm.")),
            act("flag", tag("Needs a person")),
            send("human", text("Someone from the team will reply here shortly.")),
            note("advice", "Edit the three answers to match your business, then set it live."),
        ],
        "edges": [
            ("menu", "qr:pricing", "pricing"),
            ("menu", "qr:hours", "hours"),
            ("menu", "qr:human", "flag"),
            ("flag", "default", "human"),
        ],
    },
    {
        "slug": "telegram-booking-enquiry",
        "name": "Take a booking enquiry",
        "triggers": [("keyword", "telegram", keyword("book", "booking", "appointment"))],
        "nodes": [
            send("intro", text("Happy to book you in. Three quick questions.")),
            ask("ask_name", "What name is the booking under?", key="first_name"),
            ask("ask_when", "What day and rough time suits you?", key="Preferred time", kind="custom_field"),
            ask("ask_phone", "What is the best number to reach you on?", key="phone", reply="phone"),
            act("flag", tag("Booking enquiry"), field("Signup source", "Telegram")),
            send("done", text("Thanks {{first_name}}. We will confirm the slot here shortly.")),
        ],
        "edges": chain("intro", "ask_name", "ask_when", "ask_phone", "flag", "done")
        + [("ask_when", "timeout", "flag"), ("ask_phone", "timeout", "flag")],
    },
    {
        "slug": "telegram-support-triage",
        "name": "Sort support messages",
        "triggers": [("keyword", "telegram", keyword("problem", "broken", "not working", "issue"))],
        "nodes": [
            send(
                "ask",
                text("Sorry that is not working. Which is closest?"),
                quick_replies=[
                    quick("order", "An order"),
                    quick("account", "My account"),
                    quick("other", "Something else"),
                ],
            ),
            act("order", tag("Support: order")),
            act("account", tag("Support: account")),
            act("other", tag("Support: other")),
            send("wait", text("Thanks. Someone who handles that will pick this up here.")),
        ],
        "edges": [
            ("ask", "qr:order", "order"),
            ("ask", "qr:account", "account"),
            ("ask", "qr:other", "other"),
            ("order", "default", "wait"),
            ("account", "default", "wait"),
            ("other", "default", "wait"),
        ],
    },
    {
        "slug": "sms-keyword-opt-in",
        "name": "SMS keyword opt-in",
        "triggers": [("keyword", "sms", keyword("JOIN", "START", mode="exact"))],
        "nodes": [
            send(
                "confirm",
                text(
                    "You are subscribed. Msg & data rates may apply, around 4 messages a month. Reply STOP to opt out."
                ),
            ),
            ask("ask_name", "What name should we use?", key="first_name"),
            act("record", tag("SMS subscriber"), field("Signup source", "SMS keyword")),
            send("welcome", text("Thanks {{first_name}} - you are all set.")),
        ],
        "edges": chain("confirm", "ask_name", "record", "welcome") + [("ask_name", "timeout", "record")],
    },
    {
        "slug": "sms-appointment-reminder",
        "name": "Remind someone of an appointment",
        "triggers": [("api", "", {})],
        "nodes": [
            send(
                "remind",
                text(
                    "Reminder: your appointment is tomorrow at {{Preferred time}}.",
                    "Reply R to reschedule or C to cancel.",
                ),
            ),
            wait("hold", 20, "hours"),
            send("day_of", text("See you today, {{first_name}}. Reply here if anything changes.")),
        ],
        "edges": chain("remind", "hold", "day_of"),
    },
    {
        "slug": "sms-review-request",
        "name": "Ask for a review",
        "triggers": [("api", "", {})],
        "nodes": [
            wait("settle", 2, "days"),
            send(
                "ask",
                text("Hi {{first_name}}, how did we do? A one-line review really helps us."),
                buttons=[button("Leave a review", "https://example.com/review", "review")],
            ),
            act("flag", tag("Review requested")),
            wait("chase", 5, "days"),
            send("nudge", text("No pressure at all - if you have a minute, the link is still open.")),
        ],
        "edges": chain("settle", "ask", "flag", "chase", "nudge"),
    },
    {
        "slug": "messenger-welcome",
        "name": "Messenger welcome",
        "triggers": [("welcome", "messenger", {})],
        "nodes": [
            send(
                "hello",
                text("Hi {{first_name}}! Thanks for messaging.", "What brings you here?"),
                quick_replies=[
                    quick("buy", "I want to order"),
                    quick("ask", "I have a question"),
                    quick("hours", "Are you open?"),
                ],
            ),
            send("order", text("Great. Tell me what you are after and I will check stock.")),
            send("question", text("Go ahead - what would you like to know?")),
            send("hours", text("We are open Monday to Saturday, 9am to 6pm.")),
        ],
        "edges": [
            ("hello", "qr:buy", "order"),
            ("hello", "qr:ask", "question"),
            ("hello", "qr:hours", "hours"),
        ],
    },
    {
        "slug": "messenger-quote-request",
        "name": "Collect the details for a quote",
        "triggers": [("keyword", "messenger", keyword("quote", "estimate", "how much"))],
        "nodes": [
            send("intro", text("Happy to quote. Three questions and I will pass it to the team.")),
            ask("ask_job", "What is the job?", key="Job description", kind="custom_field"),
            ask("ask_where", "Which town or postcode?", key="Postcode", kind="custom_field"),
            ask("ask_email", "Where should we send the quote?", key="email", reply="email"),
            act("flag", tag("Quote requested"), field("Signup source", "Messenger")),
            send("done", text("Thanks. You will have a quote within one working day.")),
        ],
        "edges": chain("intro", "ask_job", "ask_where", "ask_email", "flag", "done")
        + [("ask_job", "timeout", "flag"), ("ask_where", "timeout", "flag"), ("ask_email", "timeout", "flag")],
    },
    {
        "slug": "messenger-comment-to-dm",
        "name": "Reply to a Facebook comment privately",
        "triggers": [("comment", "messenger", comment_on_any("info", "details", "send"))],
        "nodes": [
            send("dm", text("Hi! You asked on the post, so here are the details.", "Anything else, just reply here.")),
            act("flag", tag("Came from a comment")),
        ],
        "edges": chain("dm", "flag"),
    },
    {
        "slug": "whatsapp-opening-hours",
        "name": "Answer when you are open",
        "triggers": [("keyword", "whatsapp", keyword("open", "hours", "opening"))],
        "nodes": [
            branch("is_open", window("whatsapp", "inside")),
            send(
                "open_now",
                text(
                    "We are open right now - Monday to Friday, 9am to 6pm.", "Ask away and someone will reply shortly."
                ),
            ),
            send(
                "closed",
                text(
                    "We are closed at the moment. We are open Monday to Friday, 9am to 6pm.",
                    "Leave your question here and we will answer first thing.",
                ),
            ),
        ],
        "edges": [("is_open", "cond:true", "open_now"), ("is_open", "cond:false", "closed")],
    },
    {
        "slug": "whatsapp-order-status",
        "name": "Check an order",
        "triggers": [("keyword", "whatsapp", keyword("order", "where is", "tracking"))],
        "nodes": [
            ask("ask_order", "Sure - what is the order number?", key="Order number", kind="custom_field"),
            act("flag", tag("Order query")),
            send("checking", text("Thanks. Someone is checking that now and will reply here.")),
            note(
                "advice",
                "Connect your shop with an External request step to answer this automatically instead of by hand.",
            ),
        ],
        "edges": chain("ask_order", "flag", "checking") + [("ask_order", "timeout", "flag")],
    },
    {
        "slug": "out-of-hours-reply",
        "name": "Reply outside opening hours",
        "triggers": [("default_reply", "", {})],
        "nodes": [
            branch("shut", window("instagram", "outside")),
            send(
                "after_hours",
                text("Thanks for your message. We are away until 9am, and you are first in the queue in the morning."),
            ),
            act("flag", tag("Arrived out of hours")),
        ],
        "edges": [("shut", "cond:true", "after_hours"), ("after_hours", "default", "flag")],
    },
    {
        "slug": "collect-an-email-address",
        "name": "Ask for an email address",
        "triggers": [("keyword", "instagram", keyword("newsletter", "subscribe", "list"))],
        "nodes": [
            branch("already", has_field("email")),
            send("known", text("You are already on the list with {{email}} - nothing to do.")),
            ask("ask_email", "What is the best email address for you?", key="email", reply="email"),
            act("subscribe", tag("Newsletter"), field("Signup source", "Chat")),
            send("welcome", text("You are on the list. One useful email a month, no more.")),
        ],
        "edges": [
            ("already", "cond:true", "known"),
            ("already", "cond:false", "ask_email"),
            ("ask_email", "default", "subscribe"),
            ("subscribe", "default", "welcome"),
        ],
    },
    {
        "slug": "waitlist-signup",
        "name": "Join a waitlist",
        "triggers": [("keyword", "instagram", keyword("waitlist", "notify", "restock"))],
        "nodes": [
            ask(
                "ask_email",
                "I will let you know the moment it is back. What is your email?",
                key="email",
                reply="email",
            ),
            act("join", tag("Waitlist"), field("Signup source", "Waitlist")),
            send("done", text("You are on the waitlist. You will hear from me first.")),
        ],
        "edges": chain("ask_email", "join", "done"),
    },
    {
        "slug": "follow-up-an-unanswered-enquiry",
        "name": "Follow up an enquiry that went quiet",
        "triggers": [("api", "", {})],
        "nodes": [
            wait("hold", 2, "days"),
            send(
                "nudge",
                text(
                    "Hi {{first_name}}, just checking you got what you needed.",
                    "Happy to help if anything is still open.",
                ),
            ),
            wait("second", 4, "days"),
            send(
                "last",
                text(
                    "I will leave it there so I am not filling your inbox.",
                    "Reply any time and I will pick it straight up.",
                ),
            ),
            act("flag", tag("Went quiet")),
        ],
        "edges": chain("hold", "nudge", "second", "last", "flag"),
    },
    {
        "slug": "hand-over-to-a-person",
        "name": "Hand over to a person",
        "triggers": [("keyword", "instagram", keyword("human", "agent", "person", "someone"))],
        "nodes": [
            send("ack", text("Of course - putting you through to a person now.")),
            act("flag", tag("Needs a person")),
            note(
                "advice",
                "Assign this to whoever is on the inbox today. The Action step "
                "can notify them as well as tag the conversation.",
            ),
        ],
        "edges": chain("ack", "flag"),
    },
    {
        "slug": "feedback-after-a-purchase",
        "name": "Ask how it went after a purchase",
        "triggers": [("api", "", {})],
        "nodes": [
            wait("settle", 3, "days"),
            send(
                "ask",
                text("How did you get on with your order, {{first_name}}?"),
                quick_replies=[quick("good", "All good"), quick("bad", "Not quite right")],
            ),
            act("happy", tag("Happy customer")),
            send(
                "review",
                text("That is good to hear. A quick review would mean a lot:"),
                buttons=[button("Leave a review", "https://example.com/review", "review")],
            ),
            act("unhappy", tag("Needs a person")),
            send("fix", text("Sorry to hear that. Tell me what went wrong and I will get it sorted.")),
        ],
        "edges": [
            ("settle", "default", "ask"),
            ("ask", "qr:good", "happy"),
            ("ask", "qr:bad", "unhappy"),
            ("happy", "default", "review"),
            ("unhappy", "default", "fix"),
        ],
    },
    {
        "slug": "event-reminder",
        "name": "Remind people about an event",
        "triggers": [("keyword", "telegram", keyword("event", "webinar", "class"))],
        "nodes": [
            send(
                "details",
                text("The next one is Thursday at 7pm, online and free."),
                buttons=[button("Save your seat", "https://example.com/event", "seat")],
            ),
            act("register", tag("Event: interested")),
            wait("before", 1, "days"),
            send("remind", text("Tomorrow at 7pm, {{first_name}}. The link arrives an hour before.")),
        ],
        "edges": chain("details", "register", "before", "remind"),
    },
    {
        "slug": "first-message-welcome",
        "name": "Welcome someone new",
        "triggers": [("welcome", "telegram", {})],
        "nodes": [
            send("hello", text("Hi {{first_name}} - good to meet you.", "I am the quick way to reach us here.")),
            act("tag_new", tag("New contact"), field("Signup source", "Chat")),
            wait("hold", 3, "hours"),
            send("offer", text("Anything you want to know, just ask. A person reads everything here too.")),
        ],
        "edges": chain("hello", "tag_new", "hold", "offer"),
    },
]


# ---------------------------------------------------------------------------
# Building and exporting
# ---------------------------------------------------------------------------

#: Nodes are laid out left to right in the order they are written, in two rows,
#: so an imported template opens on a canvas somebody can read rather than on a
#: pile at the origin. The builder never re-lays-out a graph, so this is the
#: layout every importer gets.
COLUMN = 300
ROW = 200
PER_ROW = 5


def positioned(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    placed = []
    for index, node in enumerate(nodes):
        placed.append({**node, "position": {"x": (index % PER_ROW) * COLUMN, "y": (index // PER_ROW) * ROW}})
    return placed


def connection_for(workspace: Workspace, platform: str) -> Any:
    """One scratch connection per platform, so triggers can be bound.

    Binding matters for what comes out the other end. ``_export_trigger`` reads
    the platform off the trigger's **connection**, so an unbound trigger exports
    ``platform: null`` — which SPEC §5 reads as "every platform this trigger type
    supports". A template exported that way carries no ``platform`` requirement,
    so the wizard never asks which account to watch and the flow lands listening
    on all of them. An SMS keyword template would answer Telegram.

    The connection itself never leaves: the exporter writes the platform name
    and drops the id, which is the portable half.
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus

    existing = ChannelConnection.objects.for_workspace(workspace).filter(platform=platform).first()
    if existing is not None:
        return existing
    return ChannelConnection.objects.create(
        workspace=workspace,
        platform=platform,
        external_id=f"template-authoring-{platform}",
        display_name=f"Template authoring ({platform})",
        status=ConnectionStatus.ACTIVE,
    )


def build(workspace: Workspace, spec: dict[str, Any]) -> Flow:
    """One template as a real Flow row, so the real exporter can read it."""
    graph = {
        "schema": 1,
        "nodes": positioned(spec["nodes"]),
        "edges": [
            {
                "id": f"{source}-{handle.replace(':', '-')}-{target}",
                "source": source,
                "sourceHandle": handle,
                "target": target,
            }
            for source, handle, target in spec["edges"]
        ],
    }
    flow = Flow(workspace=workspace, name=spec["name"], folder=FOLDER, status=FlowStatus.DRAFT)
    flow.save()
    FlowVersion(workspace=workspace, flow=flow, version=1, graph_json=graph).save()
    for priority, (trigger_type, platform, config) in enumerate(spec["triggers"]):
        Trigger(
            workspace=workspace,
            flow=flow,
            type=trigger_type,
            config_json=config,
            enabled=True,
            priority=priority * 10,
            # Bound here so the export carries the platform; see connection_for.
            # A type no channel delivers (api, rule) names no platform and stays
            # unbound, which is what those types mean.
            channel_connection=connection_for(workspace, platform) if platform else None,
        ).save()
    return flow


def scratch_workspace() -> Workspace:
    organization = Organization(name="Template authoring")
    organization.save()
    workspace = Workspace(organization=organization, name="Template authoring")
    workspace.save()
    return workspace


class RollbackError(Exception):
    """Raised to undo everything this script wrote."""


def main() -> int:
    directory = ROOT / "flow-templates"
    directory.mkdir(exist_ok=True)
    written: dict[str, str] = {}
    failures: list[str] = []

    try:
        with transaction.atomic():
            workspace = scratch_workspace()
            for spec in TEMPLATES:
                try:
                    flow = build(workspace, spec)
                    document = export_document(flow)
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    failures.append(f"{spec['slug']}: {type(error).__name__}: {error}")
                    continue
                # `serialize`, not json.dumps: it is the canonical form the
                # round-trip test diffs against, and its ensure_ascii=False is
                # why "£39" stays "£39" rather than becoming "\u00a339".
                written[spec["slug"]] = serialize(document)
            raise RollbackError
    except RollbackError:
        pass

    if failures:
        for line in failures:
            print(f"FAILED  {line}")
        return 1

    # Only files this script owns. apps/flows/tests/template_sources.py
    # generates the rest of the directory and gates them byte-for-byte, so a
    # blanket prune here deleted eighteen templates it had never written —
    # from the command the module docstring tells contributors to run.
    owned = {spec["slug"] for spec in TEMPLATES}
    for existing in directory.glob("*.json"):
        if existing.stem in owned and existing.stem not in written:
            existing.unlink()
            print(f"removed {existing.name}")
    for slug, body in written.items():
        (directory / f"{slug}.json").write_text(body)
    print(f"wrote {len(written)} templates to {directory.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
