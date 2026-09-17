"""Template cards, as the two surfaces that show them render them.

``library.TemplateCard`` says what a shipped template *is* — a name, a summary,
the requirement kinds it walks out to. This module says how one *reads*: labels
resolved against the registries that already own them, a start URL reversed, and
the one per-workspace fact a card carries, which is whether the channel it needs
is connected.

It lives beside ``library`` rather than in a view because two views want it —
the gallery page and the flows-list empty state — and because ``library`` must
stay workspace-free: its cards are ``lru_cache``d on file content, so nothing
about a particular workspace may reach them.
"""

from collections.abc import Sequence
from typing import Any

from django.urls import reverse

__all__ = ["REQUIREMENT_KIND_HELP", "REQUIREMENT_KIND_LABELS", "card_contexts"]


#: What each requirement kind is called, everywhere it is named. Shared by the
#: cards and by the import review page's sections, so the same thing cannot be
#: called two different names one page apart.
REQUIREMENT_KIND_LABELS: dict[str, str] = {
    "tag": "Tags",
    "custom_field": "Custom fields",
    "sequence": "Sequences",
    "segment": "Segments",
    "member": "Members",
    "flow": "Other flows",
    "media": "Media",
    "platform": "Channels",
    "request_header": "Request headers",
    "whatsapp_template": "WhatsApp templates",
    "link_handle": "Ref link handles",
    "from_override": "Email sender addresses",
    "comment_posts": "Comment trigger posts",
}


#: The sentence under each section heading on the import review page. Beside the
#: labels deliberately: the two are keyed by the same requirement kinds, and a
#: new kind that got one and not the other would render a bare slug as a heading
#: or a heading with nothing under it.
REQUIREMENT_KIND_HELP: dict[str, str] = {
    "tag": (
        "This flow labels people with the tags below, and your workspace does not have "
        "them yet. For each one: create it under this name, or point it at a tag you "
        "already use. (A tag is a label on a person — “VIP”, “Newsletter” — that you can "
        "search and filter by later.)"
    ),
    "custom_field": (
        "This flow saves these details onto a contact, and your workspace does not have "
        "them yet. For each one: create it, or point it at a field you already have. "
        "(A custom field is a detail that is not built in — a size, a booking date, an "
        "order number. Its type decides what you can store, and cannot be changed later.)"
    ),
    "sequence": (
        "A sequence is a series of messages sent over days. A new one arrives empty, so add its messages afterwards."
    ),
    "segment": "A segment is a saved contact filter and cannot be created from a file. Pick one you already have.",
    "member": "Who the flow assigns conversations to and notifies. Defaults to you.",
    "flow": "Flows this one hands over to. A file exported with everything it uses carries them along.",
    "media": "Pick an image or file from your library, or paste a link to use instead.",
    "platform": (
        "Which connected account each trigger should watch. Letting one watch every account "
        "is wider than it sounds: it covers every platform that kind of trigger works on, not "
        "just this one, so a Telegram keyword trigger would answer SMS as well."
    ),
    "request_header": "These were stripped when the file was made, so no password could travel in it. Supply your own.",
    "whatsapp_template": "The flow sends these approved templates. Nothing to answer — make sure you have them.",
    "link_handle": "The public @handle a link was built from was stripped when the file was made.",
    "from_override": "The sending address was stripped when the file was made.",
    "comment_posts": (
        "The trigger watched particular posts, and which posts was stripped when the file was made. "
        "List your own — leaving this blank does not mean every post, it means no posts, so the "
        "trigger would never fire."
    ),
}


def card_contexts(workspace: Any, cards: Sequence[Any]) -> list[dict[str, Any]]:
    """Render-ready contexts for ``cards``, in the order given.

    The connections query runs once for the batch rather than once per card, so
    pass the slice you are going to show — the empty state shows four of the
    twenty-one, and reversing twenty-one URLs to throw seventeen away is work
    nobody asked for. No cards, no query: a deployment that ships none renders
    this page too.
    """
    from apps.flows.capabilities import connected_platforms

    if not cards:
        return []
    connected = frozenset(connected_platforms(workspace))
    return [_card_context(workspace.pk, card, connected) for card in cards]


def _card_context(workspace_id: Any, card: Any, connected: frozenset[str]) -> dict[str, Any]:
    """One card with its labels resolved and its start URL reversed.

    Labels come from the registries that already own them —
    ``REQUIREMENT_KIND_LABELS``, ``Platform``'s choices, and each trigger type's
    own ``TriggerSpec.label`` — rather than from a second table: the review step
    and the trigger panel already name these things, and a gallery that called a
    channel or a trigger something else would be the same feature speaking with
    two voices.

    Each platform carries its key as well as its label, because the card says
    whether that channel is connected and the template that renders it needs the
    key for the icon. ``connected`` is baked in here rather than passed to the
    template as an ambient variable: this partial is included from two different
    pages, and one of them would have had to remember to carry it.
    """
    from apps.common.platforms import Platform
    from apps.flows.triggers.registry import spec_for

    def platform_label(key: str) -> str:
        try:
            return str(Platform(key).label)
        except ValueError:
            return key

    def trigger_label(trigger_type: str) -> str:
        spec = spec_for(trigger_type)
        return spec.label if spec is not None else trigger_type

    return {
        "card": card,
        "platforms": [
            {"key": key, "label": platform_label(key), "connected": key in connected} for key in card.platforms
        ],
        "needs": [REQUIREMENT_KIND_LABELS.get(kind, kind) for kind in card.needs],
        "triggers": [trigger_label(trigger_type) for trigger_type in card.trigger_types],
        "start_url": reverse(
            "flows:template_start",
            kwargs={"workspace_id": workspace_id, "template_slug": card.slug},
        ),
    }
