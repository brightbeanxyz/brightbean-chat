"""What the composer may offer, read out of the registries (SPEC §13.1).

Every per-platform affordance in the composer is **data from a registry**, never
a branch on a platform name. That is contract 4's promise — "a Layer-5 platform
costs one module and one registry line" — and it is why there is no
``if platform == "whatsapp"`` in this package and no ``{% if platform == … %}``
in a template. The templates ask this payload questions like
``composer.allows_buttons`` and ``composer.tag_choices``; a seventh platform
answers them by existing.

Where each answer comes from:

===========================  ==================================================
Which connections may be     ``apps.channels.policy.policy_for(p).broadcast_allowed``
picked                       — SPEC §13.2's "Instagram never appears in the
                             broadcast channel selector" is that flag being
                             False, not a name this module knows.
Which blocks render          ``apps.channels.capabilities.capabilities_for(p)``
Message tags + Meta's copy   ``policy.outside_window`` being a ``policy.NeedsTag``
Approved templates           ``apps.channels.whatsapp_templates.approved_templates_for``
Template variables           ``apps.channels.whatsapp_templates.variable_schema``
Cost hint                    ``apps.channels.whatsapp_templates.cost_hint_for``
SMS segments                 ``apps.channels.segments.segments_for``
Email suppression            ``apps.channels.suppression.is_suppressed``
Media                        ``apps.media_library.picker`` (through its endpoint)
===========================  ==================================================

Not one of those is re-implemented here. If a second table of approved
templates, a second segment counter or a second eligibility filter ever appears
in this package, it is a bug in this module first.
"""

from typing import Any

from apps.channels import policy as channel_policy
from apps.channels import segments as sms_segments
from apps.channels import whatsapp_templates
from apps.channels.capabilities import capabilities_for
from apps.channels.models import ChannelConnection, ConnectionStatus

__all__ = [
    "BLOCK_KINDS",
    "broadcastable_connections",
    "composer_config",
    "segment_hint",
    "template_options",
]

#: The block kinds a broadcast composer offers, in the order it offers them.
#: ``text`` is not in the list because every platform has it and it is the
#: composer's default block; the rest are capability-gated.
BLOCK_KINDS: tuple[str, ...] = ("image", "video", "audio", "file", "card", "gallery")


def broadcastable_connections(workspace: Any) -> list[ChannelConnection]:
    """The connections a broadcast may be sent on.

    Instagram disappears from this list because
    ``PlatformPolicy.broadcast_allowed`` is False for it (SPEC §6.3: "no tag
    that permits it"), which is the whole of SPEC §13.2's "Instagram never
    appears in the broadcast channel selector". Reading the flag rather than
    naming the platform means a future platform that forbids broadcasts is
    excluded on the day its policy row lands.

    Disabled connections are excluded too — a broadcast is scheduled now and
    sends later, and a connection nobody can send on is not a choice.
    """
    rows = (
        ChannelConnection.objects.for_workspace(workspace)
        .exclude(status=ConnectionStatus.DISABLED)
        .order_by("platform", "display_name")
    )
    return [row for row in rows if _policy(row).broadcast_allowed]


def _policy(connection: Any) -> channel_policy.PlatformPolicy:
    return channel_policy.policy_for(connection.platform)


def tag_choices(connection: Any) -> tuple[tuple[str, ...], str]:
    """The message tags this platform accepts outside its window, and Meta's copy.

    ``allowed_use_text`` is carried through **verbatim**: SPEC §6.4 requires the
    composer to display it, ``policy.NeedsTag``'s docstring says it lives beside
    the tag list because Meta revises both together, and paraphrasing a
    compliance obligation is how a page gets disabled.

    ``((), "")`` for every platform whose outside-window answer is not a tag,
    which is what makes the selector appear or not without a branch.
    """
    outside = _policy(connection).outside_window
    if isinstance(outside, channel_policy.NeedsTag):
        return outside.tags, outside.allowed_use_text
    return (), ""


def template_options(workspace: Any, connection: Any) -> list[dict[str, Any]]:
    """The approved templates a send on this connection may reference.

    ``approved_templates_for`` is the selector written for this composer by name
    — its docstring says so — and ``variable_schema`` is the shape it hands over,
    so neither "which templates are usable" nor "how is a body laid out" is
    answered twice. A platform with no templates simply returns nothing, which is
    again the affordance disappearing as data.
    """
    if not _policy(connection).has_window() or _policy(connection).outside_window != "needs_template":
        return []
    return [
        whatsapp_templates.variable_schema(template)
        for template in whatsapp_templates.approved_templates_for(workspace, connection=connection)
    ]


def segment_hint(connection: Any, text: str) -> dict[str, Any] | None:
    """SPEC §6.6's segment-count preview, or ``None`` where it does not apply.

    ``segments_for`` is pure — no Django, no clock, no price — so this adds the
    only thing it deliberately leaves out: nothing. Per-segment *price* is
    deployment data this product does not hold (SPEC §22: "OpenChat only warns,
    never meters"), so the composer shows the count and the encoding, which is
    what makes a 161-character message costing two segments explicable.

    Gated on the platform having a segment cost at all, read from the
    capabilities table rather than from a name: a platform whose ``max_text_len``
    is the SMS single-segment figure is the one this arithmetic is about.
    """
    if not _is_sms_like(connection):
        return None
    count = sms_segments.segments_for(text or "")
    return {
        "encoding": count.encoding,
        "characters": count.characters,
        "segments": count.segments,
        "remaining": count.remaining,
        "limit": count.limit,
    }


def _is_sms_like(connection: Any) -> bool:
    """Whether messages on this connection are billed by GSM-03.38 segment.

    Asked of the capabilities table: a segment-counted channel is one that
    renders text and nothing else — no media, no cards, no buttons — which is
    exactly the shape SPEC §6.6 describes and exactly what
    ``apps.channels.segments`` computes for. A platform added later that shares
    that shape gets the preview for free; one that does not, does not.
    """
    caps = capabilities_for(connection.platform)
    return caps.text and not any(
        (caps.image, caps.audio, caps.video, caps.file, caps.card, caps.gallery, caps.buttons, caps.quick_replies)
    )


def composer_config(workspace: Any, connection: Any) -> dict[str, Any]:
    """Everything the content step needs to render itself, in one payload.

    One dict rather than a dozen template variables, for the reason
    ``apps.contacts.views.filter_config`` gives: it is one ``x-data`` argument,
    and assembling it in the template would put the payload's shape somewhere
    Python cannot see it.
    """
    caps = capabilities_for(connection.platform)
    policy = _policy(connection)
    tags, allowed_use_text = tag_choices(connection)
    templates = template_options(workspace, connection)

    return {
        "connection_id": str(connection.pk),
        "connection_name": connection.display_name,
        # Carried for display and for the media picker's advisory warnings —
        # never compared against a literal to decide an affordance.
        "platform": connection.platform,
        "platform_label": connection.get_platform_display(),
        # -- what the message may contain -------------------------------------
        "blocks": [kind for kind in BLOCK_KINDS if caps.supports_block(kind)],
        "allows_buttons": caps.max_buttons > 0,
        "allows_quick_replies": caps.max_quick_replies > 0,
        "allows_url_buttons": caps.url_buttons,
        "max_buttons": caps.max_buttons,
        "max_quick_replies": caps.max_quick_replies,
        "max_text_len": caps.max_text_len,
        # WhatsApp's interactive message is either a reply-button set or a list
        # and never both; the composer disables one control set when the other
        # has entries rather than handing the adapter something it can only drop.
        "interaction_is_exclusive": caps.interaction_is_exclusive,
        # -- outside-window affordances ---------------------------------------
        "has_window": policy.has_window(),
        "window_hours": policy.window_hours,
        "tag_choices": list(tags),
        "tag_allowed_use_text": allowed_use_text,
        "needs_template_outside_window": policy.outside_window == "needs_template",
        "templates": templates,
        "cost_hint": _cost_hint(workspace, templates),
        # -- the SMS-shaped preview -------------------------------------------
        "counts_segments": _is_sms_like(connection),
    }


def _cost_hint(workspace: Any, templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The workspace's own per-category estimates, or ``None`` where irrelevant.

    Read through ``cost_hint_for``, which returns an **unsaved** instance when
    nothing has been entered — so opening a composer never writes a row. Absent
    entirely on a platform with no templates, which is how the hint stays out of
    every other composer without a branch.
    """
    if not templates:
        return None
    hint = whatsapp_templates.cost_hint_for(workspace)
    return {
        "currency": hint.currency,
        "marketing": str(hint.marketing),
        "utility": str(hint.utility),
        "authentication": str(hint.authentication),
        # Zero means "the operator has entered nothing", and the composer says
        # that rather than printing a confident 0.00 per message.
        "configured": any(amount > 0 for amount in (hint.marketing, hint.utility, hint.authentication)),
    }
