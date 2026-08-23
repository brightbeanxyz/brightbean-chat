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

    #: True when buttons and quick replies compete for **one** control set, so a
    #: message may show one kind or the other but never both.
    #:
    #: Every other platform treats the two independently, which is why this
    #: defaults to False: a Telegram message can carry an inline keyboard and
    #: quick replies, and ``max_buttons`` and ``max_quick_replies`` are then two
    #: separate budgets. WhatsApp is the exception — its `interactive` message
    #: is *either* a reply-button set (3) *or* a list (10), and there is no
    #: shape that is both.
    #:
    #: Without this the two budgets are filled independently and the adapter is
    #: handed a message it cannot represent, whose extra options it can only
    #: drop — silently, because the renderer already decided they were native
    #: and so never numbered them into the text. Declaring the exclusivity here
    #: is what lets :mod:`apps.channels.downgrade` number them instead, which is
    #: the whole point of the renderer being shared.
    interaction_is_exclusive: bool = False
    proactive_send: bool = False
    broadcast_allowed: bool = False

    # -- limits -------------------------------------------------------------
    #: 0 means the platform has no button/quick-reply support at all, which the
    #: downgrade renderer turns into numbered text options.
    max_buttons: int = 0
    max_quick_replies: int = 0
    max_text_len: int = 4096

    #: Largest attachment the platform accepts, per kind, in **bytes**. 0 means
    #: "no ceiling published here", which every consumer reads as "do not warn"
    #: rather than as "nothing may be sent" — these numbers advise, they never
    #: block (issue #16: "warn, don't block, since the target platform isn't
    #: fixed at upload").
    #:
    #: Not part of SPEC §6.1's field list, and here anyway because the
    #: alternative was a second per-platform table:
    #: ``apps.media_library.platform_limits`` carried one, with a
    #: ``TODO(#4): fold these into the contract-4 Capabilities registry`` on it,
    #: precisely because #4 had not landed when it was written. It has, and this
    #: is the fold. Whether a platform accepts a kind *at all* was already read
    #: from this table (the ``image``/``audio``/``video``/``file`` booleans), so
    #: keeping the sizes elsewhere meant one question answered from two places.
    max_image_bytes: int = 0
    max_audio_bytes: int = 0
    max_video_bytes: int = 0
    max_file_bytes: int = 0

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

    def max_bytes_for(self, kind: str) -> int:
        """The byte ceiling for one media kind, or 0 when none is published.

        Confined to :data:`_MEDIA_FLAGS` for the same reason
        :meth:`supports_block` is confined to :data:`_BLOCK_FLAGS`: the lookup
        is by name, and an unconstrained ``getattr`` would happily answer for
        ``max_text_len`` if a caller passed ``"text_len"``.
        """
        return int(getattr(self, f"max_{kind}_bytes", 0)) if kind in _MEDIA_FLAGS else 0


#: The capability names that describe a renderable block. Kept as a frozen set
#: so ``supports_block`` cannot be talked into reading ``inbound`` or
#: ``broadcast_allowed`` by passing their names as a block kind.
_BLOCK_FLAGS = frozenset({"text", "image", "audio", "video", "file", "card", "gallery"})

#: The block kinds that name a file and therefore have a byte ceiling. A subset
#: of :data:`_BLOCK_FLAGS`: text, cards and galleries are structure rather than
#: payload. Matches ``apps.media_library.mimes.MediaKind``, which is what a
#: caller passes in.
_MEDIA_FLAGS = frozenset({"image", "audio", "video", "file"})

#: Ceilings below are written in megabytes because that is how every platform
#: publishes them, and stored in bytes because that is what a file size is.
_MB = 1024 * 1024


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
        # Bot API upload ceilings for a file sent by URL.
        max_image_bytes=10 * _MB,
        max_audio_bytes=50 * _MB,
        max_video_bytes=50 * _MB,
        max_file_bytes=50 * _MB,
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
        max_image_bytes=8 * _MB,
        max_audio_bytes=25 * _MB,
        max_video_bytes=25 * _MB,
        # No max_file_bytes: `file` is False above, so there is no ceiling to
        # publish. A sibling that enables a kind adds its number in this row.
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
        max_image_bytes=25 * _MB,
        max_audio_bytes=25 * _MB,
        max_video_bytes=25 * _MB,
        max_file_bytes=25 * _MB,
    ),
    # SPEC §6.5. Cloud API interactive messages come in two shapes and the
    # numbers below are how the shared downgrade renderer picks between them:
    # `buttons` (max 3) is an `interactive.button` reply-button set, and
    # `quick_replies` (max 10) is an `interactive.list` — the same split
    # Telegram makes between an inline keyboard and a reply keyboard, for the
    # same reason. Both come back as `EventPayload.button_id`, so a flow author
    # gets the same handles either way. Body text caps at 4096; no carousel.
    # proactive_send is True only because approved templates exist — the
    # policy's "needs_template" is what actually gates it.
    Platform.WHATSAPP: Capabilities(
        image=True,
        audio=True,
        video=True,
        file=True,
        buttons=True,
        quick_replies=True,
        # Reply buttons *or* a list, never both — see the field's own note.
        # A message declaring both kinds gets the buttons natively and its
        # quick replies as numbered text.
        interaction_is_exclusive=True,
        # url_buttons False, and not an oversight. A WhatsApp *session* message
        # has no URL-button set: `interactive.button` rows are reply buttons
        # only, and the one shape that carries a link — `cta_url` — takes
        # exactly one and cannot sit beside a reply button or a list. Declaring
        # True would let the renderer fit three URL buttons into a message that
        # can natively show one, and the other two would vanish at send time
        # with nothing said. False makes the shared renderer inline them as
        # `label: url` lines, which is visible and lossless. Templates do carry
        # real URL buttons, but those are authored in the template rather than
        # on an OutboundMessage, so they are not what this flag describes.
        url_buttons=False,
        window_hours=24,
        proactive_send=True,
        broadcast_allowed=True,
        max_buttons=3,
        max_quick_replies=10,
        max_text_len=4096,
        # Cloud API media ceilings, which are lower than every sibling's and
        # are the reason the picker warns at all (issue #16).
        max_image_bytes=5 * _MB,
        max_audio_bytes=16 * _MB,
        max_video_bytes=16 * _MB,
        max_file_bytes=100 * _MB,
    ),
    # SPEC §6.6. Text plus MMS images (issue #20): no audio, video or file, no
    # rich blocks and nothing to press, so buttons and quick replies downgrade
    # to the numbered options the renderer produces and the contact types back.
    # 1600 characters is Twilio's ceiling for a concatenated message — well past
    # one segment, which apps.channels.segments is what actually prices.
    Platform.SMS: Capabilities(
        image=True,
        proactive_send=True,
        broadcast_allowed=True,
        max_text_len=1600,
        # No media ceilings: v1 SMS carries no media at all, so there is no
        # kind to publish a size for. MMS (L5-D) enables the kind and its
        # ceiling together, in this row.
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
        # Inline images only; the other kinds are False above.
        max_image_bytes=25 * _MB,
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
