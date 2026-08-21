"""Per-platform sending policy — ROADMAP contract 4, fixed shape.

The contract writes the dataclass out in full::

    PlatformPolicy is fixed now: window_hours: int|None,
    outside_window: "blocked" | NeedsTag(tags: list, allowed_use_text: str) |
    "needs_template", human_agent_days: int|None (agent sends only),
    broadcast_allowed: bool, rate_default: float.

Consumers read it as **data**. L3-A's compliance engine (`can_send`, SPEC §8) is
a single chokepoint that branches on these fields; Layer-5 adapters add a row
here and never patch the engine. L2-D reads it — with :mod:`apps.channels.capabilities`
— to warn a flow author that a send will be blocked outside the window, which is
why neither module imports adapter code.

**Reading order matters and is SPEC §8's, not this module's.** ``outside_window``
answers "what happens to a send after the window closed", a question that only
exists when ``window_hours`` is set. A consumer checks ``window_hours is None``
first and never looks at ``outside_window`` for Telegram, SMS or email. The
literal is still populated for those platforms because the contract fixes the
type as a three-way union with no "not applicable" member; :meth:`PlatformPolicy.has_window`
exists so a consumer can ask the question directly instead of comparing to None
by hand.
"""

from dataclasses import dataclass
from typing import Literal

from apps.common.platforms import Platform

__all__ = ["POLICIES", "NeedsTag", "OutsideWindow", "PlatformPolicy", "policy_for"]


@dataclass(frozen=True)
class NeedsTag:
    """Outside the window, a send needs one of ``tags``.

    ``allowed_use_text`` is Meta's own description of what the tags may be used
    for. It is carried here rather than in a template because SPEC §6.4 requires
    the broadcast composer to *display* it when an operator selects a tag — the
    text is a compliance obligation, and one that Meta revises, so it lives with
    the tag list it describes.
    """

    tags: tuple[str, ...]
    allowed_use_text: str


#: The contract's three-way union. Two string literals plus the tag case.
type OutsideWindow = Literal["blocked", "needs_template"] | NeedsTag


@dataclass(frozen=True)
class PlatformPolicy:
    """What one platform permits, as data the compliance engine branches on."""

    #: None means no messaging window: a send is never gated on recency.
    window_hours: int | None
    #: Only consulted when ``window_hours`` is set. See the module docstring.
    outside_window: OutsideWindow
    #: Meta's HUMAN_AGENT extension, in days. **Agent sends only** — automation
    #: never gets it (SPEC §8). None where the platform has no such escape.
    human_agent_days: int | None
    broadcast_allowed: bool
    #: Sends per second, the token bucket's default refill (SPEC §8).
    rate_default: float

    def has_window(self) -> bool:
        """True when sends are gated on a messaging window."""
        return self.window_hours is not None


# Meta's published allowed-use text for the non-promotional tags. Displayed
# verbatim by the broadcast composer (SPEC §6.4).
_MESSENGER_TAG_TEXT = (
    "Non-promotional only. CONFIRMED_EVENT_UPDATE: reminders for an event the person "
    "signed up for. POST_PURCHASE_UPDATE: information about a transaction they made. "
    "ACCOUNT_UPDATE: a change to their account or application. Promotional content sent "
    "under a message tag violates Meta's policy and can disable the page."
)

_HUMAN_AGENT_TEXT = (
    "HUMAN_AGENT extends the window for a human agent's reply only. Automation may never use it (SPEC §8)."
)


POLICIES: dict[str, PlatformPolicy] = {
    # SPEC §6.2: no messaging window at all. outside_window is unreachable —
    # has_window() is False — and is "blocked" rather than something permissive
    # so a consumer that reads it without checking fails closed.
    Platform.TELEGRAM: PlatformPolicy(
        window_hours=None,
        outside_window="blocked",
        human_agent_days=None,
        broadcast_allowed=True,
        rate_default=25.0,
    ),
    # SPEC §6.3 / §8: automation is Blocked outside 24h. An agent send is
    # allowed with HUMAN_AGENT within 7 days of the last inbound message.
    # broadcast_allowed False — Instagram has no tag that permits it.
    Platform.INSTAGRAM: PlatformPolicy(
        window_hours=24,
        outside_window="blocked",
        human_agent_days=7,
        broadcast_allowed=False,
        rate_default=8.0,
    ),
    # SPEC §6.4 / §8: outside 24h automation and broadcasts need one of the
    # non-promotional tags; agents get the same 7-day HUMAN_AGENT rule.
    Platform.MESSENGER: PlatformPolicy(
        window_hours=24,
        outside_window=NeedsTag(
            tags=("CONFIRMED_EVENT_UPDATE", "POST_PURCHASE_UPDATE", "ACCOUNT_UPDATE"),
            allowed_use_text=_MESSENGER_TAG_TEXT,
        ),
        human_agent_days=7,
        broadcast_allowed=True,
        rate_default=40.0,
    ),
    # SPEC §6.5 / §8: outside 24h, an approved template or nothing. No
    # HUMAN_AGENT equivalent — an agent outside the window sends a template too.
    Platform.WHATSAPP: PlatformPolicy(
        window_hours=24,
        outside_window="needs_template",
        human_agent_days=None,
        broadcast_allowed=True,
        rate_default=20.0,
    ),
    # SPEC §6.6 / §8: no window. 1/sec is per number, which is Twilio's
    # long-code throughput.
    Platform.SMS: PlatformPolicy(
        window_hours=None,
        outside_window="blocked",
        human_agent_days=None,
        broadcast_allowed=True,
        rate_default=1.0,
    ),
    # SPEC §6.7 / §8: no window; suppression and unsubscribe do the gating.
    Platform.EMAIL: PlatformPolicy(
        window_hours=None,
        outside_window="blocked",
        human_agent_days=None,
        broadcast_allowed=True,
        rate_default=10.0,
    ),
}


def policy_for(platform: str) -> PlatformPolicy:
    """The policy for ``platform``.

    Raises :class:`KeyError` rather than defaulting. A missing policy would
    otherwise read as "no window, no tags, broadcast away", which is the most
    permissive answer available and the wrong direction to guess in.
    """
    try:
        return POLICIES[platform]
    except KeyError:
        raise KeyError(
            f"No sending policy for platform {platform!r}. "
            f"Known platforms: {sorted(POLICIES)}. Adding a platform means adding it to "
            f"apps.common.platforms.Platform, apps.channels.capabilities.CAPABILITIES "
            f"and this table together."
        ) from None


# Referenced by the Instagram entry's docstring above and by L3-A's compliance
# engine when it explains a HUMAN_AGENT denial to an operator.
HUMAN_AGENT_ALLOWED_USE_TEXT = _HUMAN_AGENT_TEXT
