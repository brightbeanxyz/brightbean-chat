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
picked                       — a flag, not a name this module knows. No shipped
                             platform sets it False; one that does is excluded
                             the day its row lands.
Whether to warn that the     ``policy.outside_window`` being ``"blocked"`` on a
window is the whole gate     platform that *has* a window — :func:`window_notice`
Which blocks render          ``apps.channels.capabilities.capabilities_for(p)``
Message tags + Meta's copy   ``policy.outside_window`` being a ``policy.NeedsTag``
Approved templates           ``apps.channels.whatsapp_templates.approved_templates_for``
Template variables           ``apps.channels.whatsapp_templates.variable_schema``
Cost hint                    ``apps.channels.whatsapp_templates.cost_hint_for``
SMS segments                 ``channels:sms_segment_preview`` (L5-D's endpoint,
                             written for this composer by name), which counts
                             through ``apps.channels.segments`` and multiplies by
                             the workspace's own ``per_segment_cost``
Email suppression            ``apps.channels.suppression.is_suppressed``, asked
                             at fanout and at send by
                             :func:`apps.broadcasts.audience.suppressed` — not
                             here: whether an address is suppressed is a fact
                             about a person, not an affordance of a platform
Media                        ``apps.media_library.picker`` (through its endpoint)
===========================  ==================================================

Not one of those is re-implemented here. If a second table of approved
templates, a second segment counter or a second eligibility filter ever appears
in this package, it is a bug in this module first.
"""

from typing import Any

from apps.channels import policy as channel_policy
from apps.channels import whatsapp_templates
from apps.channels.capabilities import capabilities_for
from apps.channels.models import ChannelConnection, ConnectionStatus

__all__ = [
    "BLOCK_KINDS",
    "broadcastable_connections",
    "composer_config",
    "tag_choices",
    "template_options",
    "window_notice",
]

#: The block kinds a broadcast composer offers, in the order it offers them.
#: ``text`` is not in the list because every platform has it and it is the
#: composer's default block; the rest are capability-gated.
#:
#: ``gallery`` is deliberately absent even where the capability table allows it.
#: The schema's ``block_gallery`` requires a ``cards`` array, and this composer
#: has no multi-card editor — SPEC §13.1 writes a broadcast's content as
#: "send_message [+ buttons]" — so offering the button could only produce a
#: block the validator refuses. An affordance that cannot succeed is worse than
#: one that is absent; a gallery belongs in the flow builder, which has the
#: canvas for it.
BLOCK_KINDS: tuple[str, ...] = ("image", "video", "audio", "file", "card")


def broadcastable_connections(workspace: Any) -> list[ChannelConnection]:
    """The connections a broadcast may be sent on.

    ``PlatformPolicy.broadcast_allowed`` decides it, and every shipped platform
    sets it True — including Instagram, whose broadcasts are narrowed by its
    messaging window rather than forbidden outright (SPEC §6.3). The flag stays
    because contract 4 fixes it and because reading it rather than naming a
    platform is what excludes a future platform that forbids broadcasts on the
    day its policy row lands.

    A platform whose window closes with no way to reopen it is a thin audience,
    not an absent affordance, and :func:`window_notice` is what says so.

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


def window_notice(connection: Any) -> str:
    """Why this platform's audience will be smaller than the filter suggests.

    The composer's other per-platform affordances are things an operator can
    *act* on — pick a tag, pick a template. This one is the opposite: a platform
    whose ``outside_window`` is ``"blocked"`` offers no action at all, so the
    only honest affordance is a sentence setting the expectation before the
    counts do it for us. Without it an Instagram broadcast reads as broken: 5,000
    match the filter, 400 receive it, and the per-reason row explaining why is
    below the fold.

    Driven by the policy row, so it appears for any platform with a window and
    no escape from it and stays absent for the rest: Telegram, SMS and email
    have no window, Messenger has tags, WhatsApp has templates. The platform
    label is interpolated from the connection rather than written out, which is
    what keeps this a registry answer and not a branch.

    ``""`` where the question does not arise, which is how the notice disappears
    from five of the six composers without a condition.
    """
    policy = _policy(connection)
    if not policy.has_window() or policy.outside_window != "blocked":
        return ""
    return (
        f"{connection.get_platform_display()} has no way to reopen a messaging window once it closes, so "
        f"this broadcast can only reach people who messaged in the last {policy.window_hours} hours. "
        "Everyone else in the audience is left out, and on a large filter that is usually most of it."
    )


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
        # Not an affordance but its absence explained — see window_notice.
        "window_notice": window_notice(connection),
        "needs_template_outside_window": policy.outside_window == "needs_template",
        "templates": templates,
        "cost_hint": _cost_hint(workspace, templates),
        # -- the SMS-shaped preview -------------------------------------------
        #
        # Whether to *offer* the segment count, read from the capability table.
        # The count itself is L5-D's endpoint, which already does the arithmetic
        # and the per-segment price — reproducing either here would be the second
        # segment counter the layer-6 ground rules forbid.
        "counts_segments": caps.counts_segments,
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
