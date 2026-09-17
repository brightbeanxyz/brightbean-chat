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
    "tag": "Create them here, or point each one at a tag you already use.",
    "custom_field": "A new field needs a type; pick the one the template expects.",
    "sequence": "A new sequence arrives empty — add its steps afterwards.",
    "segment": "A segment is a saved filter and cannot be created from a template. Pick one you already have.",
    "member": "Who the flow assigns conversations to and notifies. Defaults to you.",
    "flow": "Flows this one hands over to. A bundle export carries them with it.",
    "media": "Pick an asset from your library, or paste a URL to use instead.",
    "platform": (
        "Which connection each trigger should watch. Leaving one unbound does not mean "
        "“every connection of this platform” — it means every platform that trigger type supports "
        "(SPEC §5), so a Telegram keyword trigger would also listen on SMS."
    ),
    "request_header": "Header values were removed on export so no credential could travel. Supply your own.",
    "whatsapp_template": "The flow sends these approved templates. Nothing to answer — make sure you have them.",
    "link_handle": "The public handle a ref link is built from was removed on export.",
    "from_override": "The sending address was removed on export.",
    "comment_posts": (
        "The trigger watched specific posts and their ids were removed on export. List your own — "
        "leaving it blank keeps the trigger scoped to specific posts with none listed, so it matches nothing."
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
