"""SPEC §10's trigger vocabulary, as data rather than as branches.

Two tables and nothing else. The first is the type enum, which
:class:`apps.flows.models.Trigger` stores; the second answers "which platforms
can this type ever fire on", which SPEC §10 writes as a "Channels" column and
which three separate consumers need:

* the matcher, to decide whether an unbound trigger (``channel_connection`` is
  null, meaning "all connections of a matching platform") applies to the
  connection an event arrived on;
* ``Trigger.clean()``, to refuse a binding that could never match — an
  Instagram-only trigger pointed at an SMS connection is a data error, not a
  row that silently never fires;
* :func:`apps.flows.triggers.services.platforms_for_flow`, which recomputes the
  §16 capability warnings from what a flow is actually triggered on.

Writing it once as a dict is what keeps those three from disagreeing.

**This module imports nothing from ``apps.flows``**, deliberately:
``apps.flows.models`` imports it, so anything it imported back would be a cycle.
"""

from django.db import models

from apps.common.platforms import Platform
from apps.flows.capabilities import CAPABILITIES

__all__ = [
    "COMMENT_PARENT_ID_KEY",
    "COMMENT_POST_ID_KEY",
    "COMMENT_TEXT_KEY",
    "EVENT_DRIVEN_TYPES",
    "MAX_REF_CHARS",
    "PLATFORMS_FOR_TYPE",
    "REF_PATTERN",
    "STUB_TYPES",
    "TriggerType",
    "platforms_for",
    "type_allows_platform",
]

#: Every platform that can deliver an inbound event, **derived** from the
#: capability table rather than listed here.
#:
#: SPEC §6.7 makes email outbound-only (``inbound=False``), so "all inbound
#: channels" is five platforms, not six — and a keyword trigger offered on email
#: would be a row that can never fire. Deriving it means a seventh platform
#: arriving in ``apps.channels.capabilities`` needs no edit here, and means there
#: is one answer to "can a contact talk to us on this platform" rather than two.
#:
#: Frozenset of plain strings, not ``Platform`` members, so the tables below
#: compare equal to ``connection.platform`` straight off the column.
_INBOUND_PLATFORMS: frozenset[str] = frozenset(
    str(platform) for platform, capability in CAPABILITIES.items() if capability.inbound
)


class TriggerType(models.TextChoices):
    """SPEC §10, all ten types.

    All ten exist from this issue even though only five match events today. A
    type missing from the enum cannot be stored, so a later layer adding one
    would need a migration *here* — and the whole point of shipping the enum
    complete is that L5-A and L6-A add a matcher, not a schema change.
    """

    KEYWORD = "keyword", "Keyword"
    COMMENT = "comment", "Comment"
    STORY_MENTION = "story_mention", "Story mention"
    STORY_REPLY = "story_reply", "Story reply"
    FOLLOW = "follow", "New follower"
    REF_URL = "ref_url", "Ref URL"
    DEFAULT_REPLY = "default_reply", "Default reply"
    WELCOME = "welcome", "Welcome"
    RULE = "rule", "Rule"
    API = "api", "API"


#: SPEC §10's "Channels" column. An empty set means the type is not delivered by
#: any platform at all: ``rule`` fires on internal events (contract 7, L6-A) and
#: ``api`` only through the public flow-start endpoint (#25).
PLATFORMS_FOR_TYPE: dict[str, frozenset[str]] = {
    TriggerType.KEYWORD: _INBOUND_PLATFORMS,
    TriggerType.COMMENT: frozenset({Platform.INSTAGRAM, Platform.MESSENGER}),
    TriggerType.STORY_MENTION: frozenset({Platform.INSTAGRAM}),
    TriggerType.STORY_REPLY: frozenset({Platform.INSTAGRAM}),
    TriggerType.FOLLOW: frozenset({Platform.INSTAGRAM}),
    TriggerType.REF_URL: frozenset({Platform.TELEGRAM, Platform.MESSENGER, Platform.INSTAGRAM}),
    # "per channel" in SPEC §10: a default reply is meaningful wherever a
    # contact can send anything at all.
    TriggerType.DEFAULT_REPLY: _INBOUND_PLATFORMS,
    TriggerType.WELCOME: frozenset({Platform.TELEGRAM, Platform.MESSENGER}),
    TriggerType.RULE: frozenset(),
    TriggerType.API: frozenset(),
}

#: Types a webhook can fire. ``rule`` and ``api`` are excluded because nothing
#: arriving on a channel should ever select one, and leaving them in the
#: matcher's candidate query would make that depend on a matcher returning False
#: rather than on the query.
EVENT_DRIVEN_TYPES: frozenset[str] = frozenset(PLATFORMS_FOR_TYPE) - {TriggerType.RULE, TriggerType.API}

#: Types whose matcher is registered but always declines, because the platform
#: signal it needs does not exist yet. **Pinned by a test**, the way
#: ``engine.registry.types_without_runtime()`` is: a type leaving this set is a
#: deliberate act with a test to update, not a silent behaviour change on some
#: other issue's branch.
#:
#: Empty since #17 (L5-A). It held ``story_mention``, ``story_reply`` and
#: ``follow`` while Instagram was the only platform that could deliver them and
#: no Instagram adapter existed; all three now have real matchers in
#: :mod:`apps.flows.triggers.matching`. ``follow`` is the interesting one: its
#: matcher is real and correct, and the Instagram API with Instagram Login
#: publishes no follow webhook field, so it fires only if Meta ever grants one.
#: That is SPEC §10's "degrade gracefully" rather than a stub — the difference
#: being that a stub declines an event that arrived, and this declines nothing.
STUB_TYPES: frozenset[str] = frozenset()

#: Where a comment event carries the post it was left on.
#:
#: ``apps.channels.events.EventPayload`` has ``comment_id`` but no post id and no
#: parent id, and widening that frozen dataclass means editing a module every
#: Layer-5 adapter also touches. ``payload.extra`` is the documented escape hatch
#: for exactly this — ``delivery_status`` already uses it — so these two keys are
#: the contract L5-A (Instagram) and L5-B (Messenger) fill in their parsers. The
#: comment body itself travels in ``payload.text``, like any other text.
#:
#: ``parent_id`` empty (or absent) means the comment is top level, which is what
#: SPEC §10's ``top_level_only`` switches on.
COMMENT_POST_ID_KEY = "post_id"
COMMENT_PARENT_ID_KEY = "parent_comment_id"
#: Where the comment body travels when an adapter cannot put it in
#: ``payload.text``. Checked as a fallback, so either shape works.
COMMENT_TEXT_KEY = "comment_text"


#: What a ``ref_url`` trigger's ``ref`` may contain.
#:
#: The intersection of what the three ref-carrying platforms accept, which is
#: Telegram's ``?start=`` charset — the strictest of the three. Two things fall
#: out of picking the strict one: the ref needs no percent-encoding, so the deep
#: link and the bytes inside the QR code are the same string; and a ref can never
#: carry a character that would end the query string or the attribute it is
#: rendered into.
REF_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_REF_CHARS = 64


def platforms_for(trigger_type: str) -> frozenset[str]:
    """Which platforms ``trigger_type`` can fire on. Unknown types answer empty."""
    return PLATFORMS_FOR_TYPE.get(trigger_type, frozenset())


def type_allows_platform(trigger_type: str, platform: str) -> bool:
    """May a trigger of this type be bound to a connection on ``platform``?"""
    return platform in platforms_for(trigger_type)
