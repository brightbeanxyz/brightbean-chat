"""Which trigger, if any, this event fires — SPEC §10's priority order.

    Matching runs in priority order (int, lower first), first match wins per event.

Two things carry that sentence. :attr:`apps.flows.models.Trigger.Meta.ordering`
makes "lower first" a total order rather than a partial one, and
:func:`match` stops at the first matcher that says yes rather than collecting
candidates and choosing afterwards.

The per-type matchers are a **registry**, not a chain of ``if``s, so L5-A can
make story triggers real and L6-A can bind rule triggers by registering a
callable from their own ``ready()``. Three types register a stub that always
declines (:data:`apps.flows.triggers.types.STUB_TYPES`) rather than nothing at
all, so "this type exists but cannot fire yet" is visible in
:func:`registered_matchers` instead of being an absence.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.channels.events import EventType, NormalizedEvent
from apps.flows.models import FlowStatus, FlowVersion, Trigger
from apps.flows.triggers import keywords as keyword_matching
from apps.flows.triggers.registry import spec_for
from apps.flows.triggers.types import (
    COMMENT_PARENT_ID_KEY,
    COMMENT_POST_ID_KEY,
    COMMENT_TEXT_KEY,
    MAX_REF_CHARS,
    STUB_TYPES,
    TriggerType,
)

__all__ = [
    "EVENT_TRIGGER_TYPES",
    "WELCOME_POSTBACKS",
    "MatchContext",
    "TriggerMatch",
    "candidates",
    "match",
    "register_matcher",
    "register_welcome_signal",
    "registered_matchers",
]

logger = logging.getLogger(__name__)

#: Which trigger types a given event type may select. Not an ordering — order
#: comes from ``priority`` and nothing else — but a filter, so a query never
#: loads a candidate that could not possibly apply.
#:
#: ``default_reply`` is absent because SPEC §9.3 makes it step 4, *after*
#: everything else declined, which is a stage rather than a competitor.
#: ``rule`` and ``api`` are absent because no webhook delivers them. A test pins
#: all three absences.
EVENT_TRIGGER_TYPES: dict[str, tuple[str, ...]] = {
    EventType.MESSAGE: (TriggerType.WELCOME, TriggerType.KEYWORD),
    EventType.POSTBACK: (TriggerType.WELCOME, TriggerType.REF_URL),
    EventType.REFERRAL: (TriggerType.REF_URL, TriggerType.WELCOME),
    EventType.STORY_MENTION: (TriggerType.STORY_MENTION,),
    EventType.STORY_REPLY: (TriggerType.STORY_REPLY,),
    EventType.FOLLOW: (TriggerType.FOLLOW,),
    EventType.COMMENT: (TriggerType.COMMENT,),
}

#: Button ids that mean "this person has just opened the conversation".
#: Messenger's get-started button, in the three spellings the platform has used.
WELCOME_POSTBACKS = frozenset({"get_started", "GET_STARTED", "GET_STARTED_PAYLOAD"})


@dataclass(frozen=True)
class MatchContext:
    """Everything a matcher may read — deliberately not the raw provider JSON.

    A matcher that reached into ``event.raw`` would be a platform branch inside
    ``apps.flows``, which is the thing ROADMAP contract 4 exists to prevent. What
    is here is normalised, bounded, and the same shape whatever delivered it.
    """

    event: NormalizedEvent
    connection: Any
    contact: Any | None
    text: str
    ref: str
    is_welcome: bool
    now: datetime

    @classmethod
    def from_event(
        cls,
        connection: Any,
        event: NormalizedEvent,
        *,
        contact: Any = None,
        now: datetime | None = None,
    ) -> "MatchContext":
        payload = event.payload
        text = payload.text or _extra(event, COMMENT_TEXT_KEY)
        return cls(
            event=event,
            connection=connection,
            contact=contact,
            text=keyword_matching.normalise(text),
            ref=(payload.ref or "").strip()[:MAX_REF_CHARS],
            is_welcome=_is_welcome(event),
            now=now or timezone.now(),
        )

    @property
    def post_id(self) -> str:
        """The post a comment was left on. See ``triggers.types`` for the contract."""
        return _extra(self.event, COMMENT_POST_ID_KEY)

    @property
    def is_top_level_comment(self) -> bool:
        """A comment with no parent. Absent means top level, not unknown."""
        return not _extra(self.event, COMMENT_PARENT_ID_KEY)


@dataclass(frozen=True)
class TriggerMatch:
    """The trigger that won, and what it wants the flow to start with."""

    trigger: Trigger
    variables: dict[str, Any] = field(default_factory=dict)


Matcher = Callable[[Trigger, MatchContext], bool]
WelcomeSignal = Callable[[NormalizedEvent], bool]

_MATCHERS: dict[str, Matcher] = {}
_WELCOME_SIGNALS: dict[str, WelcomeSignal] = {}


def register_matcher(trigger_type: str, matcher: Matcher, *, replace: bool = False) -> Matcher:
    """Teach the matcher how one trigger type decides. Duplicates raise."""
    if trigger_type not in TriggerType.values:
        raise ValueError(f"{trigger_type!r} is not a trigger type.")
    if trigger_type in _MATCHERS and not replace:
        raise ValueError(f"{trigger_type!r} already has a matcher; pass replace=True if that is deliberate.")
    _MATCHERS[trigger_type] = matcher
    return matcher


def registered_matchers() -> tuple[str, ...]:
    """Types with a matcher, sorted. ``api`` is never in here — a test says so."""
    return tuple(sorted(_MATCHERS))


def register_welcome_signal(platform: str, predicate: WelcomeSignal, *, replace: bool = False) -> None:
    """Add a platform's own "conversation opened" signal.

    The two defaults below are platform-agnostic and already cover Messenger's
    get-started button and a Telegram ``/start`` with no payload, assuming the
    adapter normalises them the way ``apps.channels.events`` documents. This
    exists for the adapter that does neither.
    """
    if platform in _WELCOME_SIGNALS and not replace:
        raise ValueError(f"{platform!r} already has a welcome signal.")
    _WELCOME_SIGNALS[platform] = predicate


def candidates(context: MatchContext, types: tuple[str, ...]) -> Any:
    """Every trigger that could fire for this event, in match order. One query.

    The ``Exists`` clause is not an optimisation. A trigger pointing at a flow
    with no published version would otherwise **win** the match and then swallow
    the event when ``start_flow`` raised ``FlowNotRunnableError`` — the contact
    gets silence, and the trigger that should have matched next never ran. A row
    that cannot start a flow must not be a candidate at all.
    """
    published = FlowVersion.objects.unscoped().filter(flow_id=OuterRef("flow_id"), published=True)
    # .unscoped() with a reason, per CONTRIBUTING.md: this is a correlated
    # subquery whose outer query is already scoped by for_workspace(), and it is
    # compiled into that query rather than executed on its own. Exists() rather
    # than a join so a flow with several versions cannot multiply the candidate
    # rows and need a .distinct() nobody would notice was missing.
    return (
        Trigger.objects.for_workspace(context.connection.workspace_id)
        .filter(type__in=types, enabled=True, flow__status=FlowStatus.ACTIVE)
        .filter(Q(channel_connection=context.connection) | Q(channel_connection__isnull=True))
        .filter(Exists(published))
        .select_related("flow")
    )


def match(context: MatchContext) -> TriggerMatch | None:
    """The first trigger that fires for this event, or ``None``."""
    types = EVENT_TRIGGER_TYPES.get(context.event.type, ())
    if not types:
        return None

    platform = context.connection.platform
    for trigger in candidates(context, types):
        if trigger.channel_connection_id is None:
            # SPEC §5: a null connection means "all connections of *matching*
            # platform", and matching is SPEC §10's Channels column.
            spec = spec_for(trigger.type)
            if spec is None or platform not in spec.platforms:
                continue
        matcher = _MATCHERS.get(trigger.type)
        if matcher is None:
            continue
        try:
            fired = matcher(trigger, context)
        except Exception:
            # A malformed config must not take the whole event down: the next
            # trigger in priority order may well be the one that should fire.
            # Nothing platform-supplied reaches the log line (log injection).
            logger.exception("Trigger matcher for %s failed on trigger %s", trigger.type, trigger.pk)
            continue
        if fired:
            return TriggerMatch(trigger=trigger, variables=_variables(trigger, context))
    return None


# ---------------------------------------------------------------------------
# The matchers that ship with this issue
# ---------------------------------------------------------------------------


def _match_keyword(trigger: Trigger, context: MatchContext) -> bool:
    return keyword_matching.matches_any(context.text, trigger.config_json.get("keywords") or ())


def _match_ref_url(trigger: Trigger, context: MatchContext) -> bool:
    """Exact ref, per SPEC §10. Case-sensitive: a ref is an identifier, not prose."""
    ref = trigger.config_json.get("ref")
    return bool(context.ref) and isinstance(ref, str) and context.ref == ref.strip()


def _match_welcome(trigger: Trigger, context: MatchContext) -> bool:
    return context.is_welcome


def _match_comment(trigger: Trigger, context: MatchContext) -> bool:
    """The platform-agnostic half of SPEC §10's comment trigger.

    Everything here is decided from the normalised event: post scope, the two
    keyword lists and the top-level rule. What L5-A and L5-B add is a parser that
    fills ``payload.extra`` (see :mod:`apps.flows.triggers.types`), the public
    reply, the like, and the post picker — not a second copy of this.

    The once-per-contact-per-post guard is deliberately **not** here: it is a
    write, and a matcher must be free of side effects so that a later trigger
    winning does not leave a claim behind from an earlier one that lost. The
    routing stage takes it, through :mod:`apps.flows.triggers.comments`.
    """
    config = trigger.config_json
    if config.get("post_scope") == "specific":
        post_ids = config.get("post_ids") or ()
        if not context.post_id or context.post_id not in post_ids:
            return False
    if config.get("top_level_only") and not context.is_top_level_comment:
        return False

    exclude = _plain_keywords(config.get("exclude_keywords"))
    if exclude and keyword_matching.matches_any(context.text, exclude):
        return False
    include = _plain_keywords(config.get("include_keywords"))
    if include:
        return keyword_matching.matches_any(context.text, include)
    return True


def _decline(trigger: Trigger, context: MatchContext) -> bool:
    """A registered stub that always declines — the three Instagram-only types.

    Registered rather than left absent so ``registered_matchers()`` shows the
    type exists and cannot fire yet, and so L5-A's change is one
    ``register_matcher(..., replace=True)`` line in its own ``ready()`` rather
    than an edit here. ``story_reply``'s keyword config is already validated and
    stored; only the deciding is deferred.
    """
    return False


register_matcher(TriggerType.KEYWORD, _match_keyword)
register_matcher(TriggerType.REF_URL, _match_ref_url)
register_matcher(TriggerType.WELCOME, _match_welcome)
register_matcher(TriggerType.COMMENT, _match_comment)
for _stub in sorted(STUB_TYPES):
    register_matcher(_stub, _decline)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plain_keywords(values: Any) -> tuple[dict[str, str], ...]:
    """SPEC §10's comment keyword lists are bare strings; `contains` is the rule."""
    if not isinstance(values, list | tuple):
        return ()
    return tuple({"text": value, "mode": "contains"} for value in values if isinstance(value, str))


def _variables(trigger: Trigger, context: MatchContext) -> dict[str, Any]:
    """What the started flow can read as ``{{trigger_ref}}`` and friends."""
    variables: dict[str, Any] = {"trigger_type": trigger.type}
    if context.ref:
        variables["trigger_ref"] = context.ref
    return variables


def _extra(event: NormalizedEvent, key: str) -> str:
    value = event.payload.extra.get(key)
    return value.strip() if isinstance(value, str) else ""


def _is_welcome(event: NormalizedEvent) -> bool:
    """Has this contact just opened the conversation?

    Two platform-agnostic rules, then whatever an adapter registered. A referral
    carrying *no* ref is the normalised shape of "arrived with no payload", which
    is SPEC §10's Telegram ``/start`` case; a get-started postback is Messenger's.
    """
    signal = _WELCOME_SIGNALS.get(event.connection.platform if event.connection else "")
    if signal is not None and signal(event):
        return True
    if event.type == EventType.REFERRAL:
        return not (event.payload.ref or "").strip()
    if event.type == EventType.POSTBACK:
        return (event.payload.button_id or "").strip() in WELCOME_POSTBACKS
    return False
