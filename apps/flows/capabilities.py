"""Per-platform channel capabilities — ROADMAP contract 4's consumer side.

Validation emits *warnings* when a graph asks a channel for something it cannot
do: buttons on SMS, a gallery on WhatsApp, 3000 characters where the platform
takes 1000. Contract 4 is explicit that this must read **registry data, never
adapter code** — "so L2-D's validation can emit capability warnings without
importing adapter code and Layer 5 adapters never patch can_send". Importing
``channels.providers`` here would also be an import cycle waiting to happen, and
would drag HTTP clients into a code path that only ever asks a table a question.

Issue #4 has not merged, so this module is the single swap point, in the same
shape as :mod:`apps.flows.schema.condition`: the import wins as soon as
``apps.channels`` exists, and the table below is SPEC §6.1's field list filled in
from §§6.2–6.7 until then.

The values are conservative by design. A missing warning is a surprise at
runtime; a spurious one is a line in a panel that publishes anyway, since
capability findings never block (SPEC §9.1: "channel-capability warnings
(non-blocking)").
"""

from dataclasses import dataclass
from typing import Any

from apps.common.platforms import Platform

__all__ = [
    "BLOCK_TYPES",
    "CAPABILITIES",
    "CAPABILITIES_ARE_VENDORED",
    "Capabilities",
    "capabilities_for",
    "connected_platforms",
]


#: The send_message block types SPEC §11.1 defines. Named here so
#: :meth:`Capabilities.supports_block` answers only about block types: resolving
#: a graph-supplied string straight against the dataclass would happily report
#: that a platform "supports" ``max_text_len`` or ``inbound``, which are not
#: block types at all. ``test_capabilities.py`` asserts this stays equal to the
#: set the schema's message_block union declares, so the two cannot drift.
BLOCK_TYPES = frozenset({"text", "image", "audio", "video", "file", "card", "gallery"})


@dataclass(frozen=True)
class Capabilities:
    """SPEC §6.1's capability record: booleans plus limits, static per platform."""

    text: bool = True
    image: bool = False
    audio: bool = False
    video: bool = False
    file: bool = False
    card: bool = False
    gallery: bool = False
    buttons: bool = False
    quick_replies: bool = False
    url_buttons: bool = False
    typing_indicator: bool = False
    proactive_send: bool = False
    window_hours: int | None = None
    tags_supported: tuple[str, ...] = ()
    max_buttons: int = 0
    max_quick_replies: int = 0
    max_text_len: int = 4096
    broadcast_allowed: bool = False
    inbound: bool = True

    def supports_block(self, block_type: str) -> bool:
        """Whether a send_message block of this kind renders natively.

        The flag is read off this record rather than listed a second time — the
        block names and the capability names are deliberately the same — but the
        lookup is confined to :data:`BLOCK_TYPES` so that anything else answers
        False instead of whatever field happened to share the name.
        """
        if block_type not in BLOCK_TYPES:
            return False
        return bool(getattr(self, block_type, False))


_VENDORED: dict[str, Capabilities] = {
    # Bot API. Inline keyboards and reply keyboards, no native card/gallery —
    # the adapter downgrades those to image + text (SPEC §6.1). No messaging
    # window; proactive sends allowed once the contact has messaged the bot.
    Platform.TELEGRAM: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        typing_indicator=True,
        proactive_send=True,
        max_buttons=10,
        max_quick_replies=12,
        max_text_len=4096,
        broadcast_allowed=True,
    ),
    # 24h window, HUMAN_AGENT extends to 7 days for agent sends only.
    # proactive_send false and broadcast_allowed false (SPEC §6.3).
    Platform.INSTAGRAM: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        card=True,
        gallery=True,
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        typing_indicator=True,
        window_hours=24,
        tags_supported=("HUMAN_AGENT",),
        max_buttons=3,
        max_quick_replies=13,
        max_text_len=1000,
    ),
    # Same shape as Instagram, but broadcasts are allowed with a message tag
    # (SPEC §6.4).
    Platform.MESSENGER: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        card=True,
        gallery=True,
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        typing_indicator=True,
        window_hours=24,
        tags_supported=("HUMAN_AGENT", "CONFIRMED_EVENT_UPDATE", "POST_PURCHASE_UPDATE", "ACCOUNT_UPDATE"),
        max_buttons=3,
        max_quick_replies=13,
        max_text_len=2000,
        broadcast_allowed=True,
    ),
    # Cloud API interactive messages: up to 3 reply buttons or a 10-row list.
    # Outside the 24h window only approved templates go out (SPEC §6.5).
    Platform.WHATSAPP: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        proactive_send=True,
        window_hours=24,
        max_buttons=3,
        max_quick_replies=10,
        max_text_len=4096,
        broadcast_allowed=True,
    ),
    # Twilio. Text and MMS media only — no buttons, no quick replies, and the
    # segment maths is why the length ceiling is what it is (SPEC §6.6).
    Platform.SMS: Capabilities(
        image=True,
        proactive_send=True,
        max_text_len=1600,
        broadcast_allowed=True,
    ),
    # Outbound only (SPEC §6.7): no inbound, no structured controls.
    Platform.EMAIL: Capabilities(
        image=True,
        file=True,
        proactive_send=True,
        max_text_len=100_000,
        broadcast_allowed=True,
        inbound=False,
    ),
}

_imported: dict[str, Any] | None
try:  # pragma: no cover - the branch taken depends on whether #4 has merged
    # `channels.capabilities`, not `channels.registry`: contract 4 puts the
    # table in a module that imports no adapter code, and the registry only
    # borrows `capabilities_for` from it. Importing the wrong name would raise
    # ImportError, be swallowed here, and leave the vendored numbers silently in
    # force for good — which is the one failure this whole swap point exists to
    # avoid. `test_capabilities.py` asserts the swap actually took.
    from apps.channels.capabilities import CAPABILITIES as _CHANNELS_CAPABILITIES

    _imported = dict(_CHANNELS_CAPABILITIES)
except ImportError:
    _imported = None

#: True while the table above is in force. A test asserts on it so "#4 merged
#: but the import silently did not take" fails the build.
CAPABILITIES_ARE_VENDORED = _imported is None

CAPABILITIES: dict[str, Any] = _imported if _imported is not None else dict(_VENDORED)


def capabilities_for(platform: str) -> Any | None:
    """The capability record for a platform, or ``None`` for one we do not know."""
    return CAPABILITIES.get(platform)


def connected_platforms(workspace: Any) -> tuple[str, ...]:
    """Which platforms this workspace has a live connection on.

    **Documented stub until #4.** ``channels.ChannelConnection`` is that issue's
    model; until it exists there is nothing to ask, so this returns an empty
    tuple and no capability warning is ever emitted in a running deployment. The
    validator takes the platform set as an argument precisely so the rules can
    be — and are — tested against real capability data today
    (``apps/flows/tests/test_capabilities.py``).
    """
    from apps.flows.compat import installed_model

    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:
        return ()
    rows = model.objects.for_workspace(workspace).filter(status="active").values_list("platform", flat=True)
    return tuple(sorted(set(rows)))
