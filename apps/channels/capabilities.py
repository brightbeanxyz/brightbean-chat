"""The static per-platform capability table (SPEC §6.1, ROADMAP contract 4).

**This module imports no adapter code and touches no database.** That is the
whole point of it: ROADMAP contract 4 requires the capability table to ship "as
data so L2-D's validation can read it without importing adapter code". The flow
builder (#6/#10) needs to warn that a three-button card will arrive on WhatsApp
as numbered text long before any adapter exists, and an import that dragged in
``providers/`` would make that a circular dependency the first time an adapter
needed to look at a flow.

So: `apps.common.platforms` and the standard library. Nothing else, ever.

The numbers are platform limits, not preferences. Each one carries the reason it
is what it is, because the failure mode of a wrong limit is silent — a message
that the platform rejects at send time, in production, on somebody else's
account. Where a platform documents no hard cap, the comment says so and the
value is a usability choice.

:data:`Capabilities.window_hours` and :data:`Capabilities.broadcast_allowed`
also appear on :class:`~apps.channels.policy.PlatformPolicy`. The policy is
authoritative — it is what the compliance engine (L3-A) reads — and these copies
exist because SPEC §6.1 puts them in the capability list that the builder shows.
``apps/channels/tests/test_registry.py`` asserts the two never drift.
"""

from dataclasses import dataclass

from apps.common.platforms import Platform

__all__ = ["CAPABILITIES", "Capabilities", "capabilities_for"]


@dataclass(frozen=True)
class Capabilities:
    """What one platform can carry (SPEC §6.1: "booleans plus limits").

    Frozen because these are module-level singletons shared by every request in
    the worker: a mutable dataclass here means one adapter's ``capabilities.text
    = False`` silently reconfigures the whole deployment. Layer-5 adapters read
    this table; they never patch it (contract 4).
    """

    # -- block types the platform renders natively --------------------------
    text: bool = True
    image: bool = False
    audio: bool = False
    video: bool = False
    file: bool = False
    card: bool = False
    gallery: bool = False

    # -- interaction --------------------------------------------------------
    buttons: bool = False
    quick_replies: bool = False
    url_buttons: bool = False
    typing_indicator: bool = False

    # -- sending rules ------------------------------------------------------
    #: None means "no messaging window" — the platform accepts a send at any
    #: time. See PlatformPolicy, which is authoritative for compliance.
    window_hours: int | None = None
    #: Message tags that extend or replace the window, e.g. Meta's HUMAN_AGENT.
    tags_supported: tuple[str, ...] = ()
    proactive_send: bool = False
    broadcast_allowed: bool = False

    # -- limits -------------------------------------------------------------
    #: 0 means the platform has no button/quick-reply support at all, which the
    #: downgrade renderer turns into numbered text options.
    max_buttons: int = 0
    max_quick_replies: int = 0
    max_text_len: int = 4096

    #: False for a send-only channel. Email is the one in v1: SPEC §6.7 is
    #: "outbound only", and its webhook route carries bounce notifications
    #: rather than inbound messages.
    inbound: bool = True

    def supports_block(self, kind: str) -> bool:
        """True when ``kind`` renders natively. Unknown kinds are unsupported.

        Unknown-means-no is deliberate: a block type added by a later issue
        without a capability flag downgrades to text rather than being passed
        through to a platform that will reject it.
        """
        return bool(getattr(self, kind, False)) if kind in _BLOCK_FLAGS else False


#: The capability names that describe a renderable block. Kept as a frozen set
#: so ``supports_block`` cannot be talked into reading ``inbound`` or
#: ``broadcast_allowed`` by passing their names as a block kind.
_BLOCK_FLAGS = frozenset({"text", "image", "audio", "video", "file", "card", "gallery"})


# Meta's non-promotional message tags (SPEC §6.4). HUMAN_AGENT is listed
# separately per platform because Instagram supports it and nothing else.
_HUMAN_AGENT = "HUMAN_AGENT"
_MESSENGER_TAGS = (
    _HUMAN_AGENT,
    "CONFIRMED_EVENT_UPDATE",
    "POST_PURCHASE_UPDATE",
    "ACCOUNT_UPDATE",
)


CAPABILITIES: dict[str, Capabilities] = {
    # SPEC §6.2. Bot API: sendMessage caps text at 4096 characters. No
    # messaging window and no tags — the only gate is that the contact has
    # messaged the bot once, which is enforced on the identity's opt_in.
    # Inline keyboards have no documented button cap; 10 keeps a keyboard
    # usable and keeps the numbered fallback readable.
    Platform.TELEGRAM: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        # No native card/carousel: Telegram would need one message per card
        # anyway, which is exactly what the downgrade renderer produces.
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        typing_indicator=True,
        proactive_send=True,
        broadcast_allowed=True,
        max_buttons=10,
        max_quick_replies=10,
        max_text_len=4096,
    ),
    # SPEC §6.3. IG DM text limit is 1000; generic template allows 3 buttons
    # and 13 quick replies (Meta's messaging limits, shared with Messenger).
    # No generic file attachments. proactive_send False: outside the 24h
    # window automation is Blocked, and the HUMAN_AGENT extension is an
    # agent-only path the compliance engine owns.
    Platform.INSTAGRAM: Capabilities(
        image=True,
        audio=True,
        video=True,
        card=True,
        gallery=True,
        buttons=True,
        quick_replies=True,
        url_buttons=True,
        typing_indicator=True,
        window_hours=24,
        tags_supported=(_HUMAN_AGENT,),
        max_buttons=3,
        max_quick_replies=13,
        max_text_len=1000,
    ),
    # SPEC §6.4. Same Meta messaging surface with file attachments, a 2000
    # character body, and the non-promotional tag set that makes broadcasts
    # possible outside the window.
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
        tags_supported=_MESSENGER_TAGS,
        broadcast_allowed=True,
        max_buttons=3,
        max_quick_replies=13,
        max_text_len=2000,
    ),
    # SPEC §6.5. Cloud API interactive messages: at most 3 reply buttons and
    # exactly one CTA URL button, no quick replies, no carousel. Body text caps
    # at 4096. proactive_send is True only because approved templates exist —
    # the policy's "needs_template" is what actually gates it.
    Platform.WHATSAPP: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        buttons=True,
        url_buttons=True,
        window_hours=24,
        proactive_send=True,
        broadcast_allowed=True,
        max_buttons=3,
        max_text_len=4096,
    ),
    # SPEC §6.6. Text only in v1 — no MMS, no rich blocks, nothing to press.
    # 1600 characters is Twilio's ceiling for a concatenated message.
    Platform.SMS: Capabilities(
        proactive_send=True,
        broadcast_allowed=True,
        max_text_len=1600,
    ),
    # SPEC §6.7, outbound only. Inline images and hyperlinks, no buttons and no
    # inbound messages: the /webhooks/email/ route carries bounce
    # notifications. The length cap is a sanity bound on an HTML body, not a
    # protocol limit.
    Platform.EMAIL: Capabilities(
        image=True,
        url_buttons=True,
        proactive_send=True,
        broadcast_allowed=True,
        max_text_len=100_000,
        inbound=False,
    ),
}


def capabilities_for(platform: str) -> Capabilities:
    """The capability record for ``platform``.

    Raises :class:`KeyError` for an unknown platform rather than returning a
    permissive default: a typo that yielded "everything is supported" would
    surface as a rejected send against a live account, days later.
    """
    try:
        return CAPABILITIES[platform]
    except KeyError:
        raise KeyError(
            f"No capability record for platform {platform!r}. "
            f"Known platforms: {sorted(CAPABILITIES)}. Adding a platform means "
            f"adding it to apps.common.platforms.Platform, this table and "
            f"apps.channels.policy.POLICIES together."
        ) from None
