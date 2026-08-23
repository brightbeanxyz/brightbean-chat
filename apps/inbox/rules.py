"""The inbox rules engine: what a rule is, and whether it matches (SPEC §14).

One matcher, two callers. The ``post_persist`` hook scores a
:class:`~apps.channels.events.NormalizedEvent` on its way through the webhook;
the settings page's dry-run scores the last fifty stored ``Message`` rows. If
those were two implementations they would drift, and the dry-run's whole claim is
that it agrees with live behaviour — so both build a :class:`RuleInput` and both
call :func:`matches`, exactly as
:meth:`apps.flows.triggers.matching.MatchContext.from_event` exists so the
matcher never sees a platform payload.

Three halves to a condition, and the split is the issue's:

``channel``
    Rule-local. Two independent filters — platform and connection — ANDed, which
    is what a form with two multi-selects means. An empty list is not a
    constraint.

``keywords``
    Rule-local, and **not** a second keyword matcher.
    :mod:`apps.flows.triggers.keywords` already implements SPEC §10's three modes
    with the casefolding and the 8 kB scan cap, and the ``{"text", "mode"}``
    entries here are the same shape a keyword trigger stores. That module imports
    nothing from this project, so borrowing it costs no coupling.

``contact``
    The condition engine's, through ``apps.contacts.conditions`` — ROADMAP
    contract 8 names inbox rules as one of its four consumers, and its own
    docstring names issue #24. Set-wise in the dry-run
    (:func:`~apps.contacts.conditions.evaluate_many`), one contact at a time in
    the hook. There is no third evaluator.

A rule matches when **every clause it carries** matches. A clause it does not
carry is not a constraint — but a rule carrying no clause at all would match
every inbound message in the workspace, and since the builder's empty state is
exactly that, :func:`validate_condition` refuses to save one. The condition
engine has the same footgun by itself (``match: all`` over zero rules is the
identity of AND, and matches everyone); this is the one place it can be created
by accident.
"""

from dataclasses import dataclass
from typing import Any

from apps.contacts import conditions
from apps.flows.triggers import keywords as keyword_matching
from apps.flows.triggers.schema import MAX_KEYWORD_CHARS, MAX_KEYWORDS

__all__ = [
    "ACTION_TYPES",
    "MAX_ACTIONS",
    "MAX_CONDITION_BYTES",
    "MAX_CONNECTIONS",
    "RULE_EVENTS",
    "CompiledRule",
    "RuleInput",
    "RuleValidationError",
    "compile_rule",
    "matches",
    "matches_shallow",
    "validate_actions",
    "validate_condition",
]

#: The event types a rule is offered. SPEC §14 says "on inbound message", and the
#: exclusions carry the meaning — the same reasoning
#: ``apps.flows.triggers.stages.DEFAULT_REPLY_EVENTS`` gives for its own narrower
#: set. A postback is a button press and carries no text; a follow or a referral
#: is not a message; a comment has neither a contact nor a conversation to label,
#: because ``apps.messaging.ingest`` deliberately creates neither. An opt-out
#: never reaches this stage at all: ``stages.opt_out_event`` consumes it first.
RULE_EVENTS = frozenset({"message", "story_reply"})

#: Action verbs, and the whole vocabulary. SPEC §5: "add label, assign to member,
#: mark done". Round-robin assignment is explicitly out of scope for this issue.
ACTION_TYPES = ("add_label", "assign_to_member", "mark_done")

MAX_ACTIONS = 10
MAX_CONNECTIONS = 50
#: The whole condition document. Parsed on every inbound event on every
#: connection in the deployment, so this is a latency budget as much as
#: SECURITY-BASELINE §7's size cap. The contact half enforces its own
#: (``conditions.MAX_FILTER_BYTES``) on top.
MAX_CONDITION_BYTES = 16384

_CONDITION_KEYS = frozenset({"channel", "keywords", "contact"})
_CHANNEL_KEYS = frozenset({"platforms", "connection_ids"})
_KEYWORD_KEYS = frozenset({"text", "mode"})
_KEYWORD_MODES = frozenset({"exact", "contains", "any_word"})


class RuleValidationError(ValueError):
    """A rule document this engine refuses to store."""


@dataclass(frozen=True)
class RuleInput:
    """The one thing a rule is scored against, however it was assembled.

    Frozen, and holding values rather than rows: a matcher that took a
    ``NormalizedEvent`` could not be pointed at history, and one that took a
    ``Message`` could not be pointed at a webhook. Both constructors normalise
    the text through :func:`apps.flows.triggers.keywords.normalise`, so the
    haystack is identical whichever door it came in by.
    """

    text: str
    platform: str
    connection_id: str
    contact: Any = None

    @classmethod
    def from_event(cls, context: Any) -> "RuleInput":
        """A live inbound event, mid-routing."""
        return cls(
            text=keyword_matching.normalise(getattr(context.event.payload, "text", "")),
            platform=str(context.connection.platform),
            connection_id=str(context.connection.pk),
            contact=context.contact,
        )

    @classmethod
    def from_message(cls, message: Any) -> "RuleInput":
        """A stored message, for the dry-run.

        The body is read back through
        :func:`apps.messaging.rendering.outbound_from_body` rather than walked
        here — that function is already the one parser for this shape, and a
        third walker beside it and :func:`apps.inbox.rendering.render_message`
        would be a third answer to "what does this body say".
        """
        from apps.messaging.rendering import outbound_from_body

        blocks = outbound_from_body(message.body).blocks
        text = " ".join(getattr(block, "text", "") for block in blocks if block.kind == "text")
        return cls(
            text=keyword_matching.normalise(text),
            platform=str(message.channel_connection.platform),
            connection_id=str(message.channel_connection_id),
            contact=message.conversation.contact,
        )


@dataclass(frozen=True)
class CompiledRule:
    """A rule's condition, parsed once instead of once per message.

    The dry-run scores fifty messages against every rule; re-reading the same
    JSON fifty times to answer the same question is the sort of cost that only
    shows up on the page nobody profiled.
    """

    rule_id: Any
    platforms: frozenset[str]
    connection_ids: frozenset[str]
    keywords: tuple[dict[str, str], ...]
    contact_filter: dict[str, Any] | None

    @property
    def has_contact_clause(self) -> bool:
        return self.contact_filter is not None


def compile_rule(rule: Any) -> CompiledRule:
    """Read one stored rule into the form :func:`matches` wants."""
    document = rule.condition_json if isinstance(rule.condition_json, dict) else {}
    raw_channel = document.get("channel")
    channel: dict[str, Any] = raw_channel if isinstance(raw_channel, dict) else {}
    raw_contact = document.get("contact")
    contact: dict[str, Any] | None = raw_contact if isinstance(raw_contact, dict) else None
    return CompiledRule(
        rule_id=rule.pk,
        platforms=frozenset(_strings(channel.get("platforms"))),
        connection_ids=frozenset(_strings(channel.get("connection_ids"))),
        keywords=tuple(item for item in _list(document.get("keywords")) if isinstance(item, dict)),
        contact_filter=contact or None,
    )


def matches_shallow(compiled: CompiledRule, rule_input: RuleInput) -> bool:
    """The clauses that need no database — channel and keyword.

    Public because the dry-run needs the halves apart: it runs this per message,
    then asks the condition engine **once per rule** about the contacts that
    survived, instead of once per message per rule. Fifty messages and five rules
    is five queries that way and two hundred and fifty the other.
    """
    if compiled.platforms and rule_input.platform not in compiled.platforms:
        return False
    if compiled.connection_ids and rule_input.connection_id not in compiled.connection_ids:
        return False
    return not compiled.keywords or keyword_matching.matches_any(rule_input.text, compiled.keywords)


def matches(compiled: CompiledRule, rule_input: RuleInput) -> bool:
    """Whether this rule fires for this message. The hook's question."""
    if not matches_shallow(compiled, rule_input):
        return False
    if compiled.contact_filter is None:
        return True
    if rule_input.contact is None:
        # A clause about the contact cannot be true of nobody. Deliberately not
        # "matches anyway": ``conditions``' negatives include absence, so a
        # ``has_not`` rule would otherwise fire for an event with no contact at
        # all, which is a different statement from the one the operator wrote.
        return False
    return conditions.evaluate(rule_input.contact, compiled.contact_filter)


# ---------------------------------------------------------------------------
# Validation — what may be stored
# ---------------------------------------------------------------------------


def validate_condition(workspace: Any, condition_json: Any) -> dict[str, Any]:
    """Normalise and check a condition document, or raise.

    Returns the document to store rather than mutating the input: the caller
    saves what came back, so a normalisation the validator applied cannot be
    lost between the check and the write.

    Unknown keys are **named and rejected**, never dropped — SECURITY-BASELINE
    §7's mass-assignment guard, and the rule ``apps.contacts.conditions``
    already applies to its own half.
    """
    import json

    if not isinstance(condition_json, dict):
        raise RuleValidationError("A rule condition must be an object.")
    unknown = sorted(set(condition_json) - _CONDITION_KEYS)
    if unknown:
        raise RuleValidationError(f"Unknown condition keys: {', '.join(unknown)}.")
    if len(json.dumps(condition_json).encode("utf-8")) > MAX_CONDITION_BYTES:
        raise RuleValidationError("That condition is too large.")

    document: dict[str, Any] = {}

    channel = _channel(condition_json.get("channel"))
    if channel:
        document["channel"] = channel

    keywords = _keywords(condition_json.get("keywords"))
    if keywords:
        document["keywords"] = keywords

    contact = condition_json.get("contact")
    if contact:
        # Raises ConditionValidationError, which carries a path and a code the
        # form renders. Not caught and re-wrapped: the engine's message is more
        # specific than anything this module could say about it.
        conditions.validate(workspace, contact)
        document["contact"] = contact

    if not document:
        # The empty-document footgun. ``match: all`` over zero rules is the
        # identity of AND and matches everyone, and the builder's initial state
        # is exactly that — so "label every inbound message in the workspace"
        # would otherwise be one accidental save away.
        raise RuleValidationError(
            "A rule needs at least one condition: a channel, a keyword or something about the contact."
        )
    return document


def validate_actions(workspace: Any, actions_json: Any) -> list[dict[str, Any]]:
    """Normalise and check an action list, or raise.

    Every id is resolved **inside this workspace**, so a form naming another
    tenant's label or a member of another workspace is refused here rather than
    stored and acted on later by a hook that has no request to check against.
    """
    from apps.inbox.models import ConversationLabel

    if not isinstance(actions_json, list):
        raise RuleValidationError("Rule actions must be a list.")
    if not actions_json:
        raise RuleValidationError("A rule needs at least one action.")
    if len(actions_json) > MAX_ACTIONS:
        raise RuleValidationError(f"A rule may have at most {MAX_ACTIONS} actions.")

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in actions_json:
        if not isinstance(item, dict):
            raise RuleValidationError("Each action must be an object.")
        kind = item.get("type")
        if kind not in ACTION_TYPES:
            raise RuleValidationError(f"Unknown action {kind!r}.")
        if kind == "add_label":
            label_id = str(item.get("label_id") or "")
            if not ConversationLabel.objects.for_workspace(workspace).filter(pk=_uuid(label_id)).exists():
                raise RuleValidationError("That label no longer exists.")
            action = {"type": kind, "label_id": label_id}
            fingerprint = f"add_label:{label_id}"
        elif kind == "assign_to_member":
            user_id = str(item.get("user_id") or "")
            if not _is_member(workspace, user_id):
                raise RuleValidationError("That person is not a member of this workspace.")
            action = {"type": kind, "user_id": user_id}
            # One assignee, whichever action list order says. Two would be a rule
            # whose outcome depended on which one ran last.
            fingerprint = "assign_to_member"
        else:
            action = {"type": kind}
            fingerprint = kind
        if fingerprint in seen:
            raise RuleValidationError("That rule repeats an action.")
        seen.add(fingerprint)
        out.append(action)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise RuleValidationError("The channel condition must be an object.")
    unknown = sorted(set(value) - _CHANNEL_KEYS)
    if unknown:
        raise RuleValidationError(f"Unknown channel keys: {', '.join(unknown)}.")

    from apps.common.platforms import Platform

    platforms = _strings(value.get("platforms"))
    for platform in platforms:
        if platform not in Platform.values:
            raise RuleValidationError(f"Unknown platform {platform!r}.")
    connection_ids = _strings(value.get("connection_ids"))
    if len(connection_ids) > MAX_CONNECTIONS:
        raise RuleValidationError("Too many connections in one rule.")

    out: dict[str, Any] = {}
    if platforms:
        out["platforms"] = sorted(set(platforms))
    if connection_ids:
        out["connection_ids"] = sorted(set(connection_ids))
    return out


def _keywords(value: Any) -> list[dict[str, str]]:
    if not value:
        return []
    if not isinstance(value, list):
        raise RuleValidationError("Keywords must be a list.")
    if len(value) > MAX_KEYWORDS:
        raise RuleValidationError(f"A rule may have at most {MAX_KEYWORDS} keywords.")

    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuleValidationError("Each keyword must be an object.")
        unknown = sorted(set(item) - _KEYWORD_KEYS)
        if unknown:
            raise RuleValidationError(f"Unknown keyword keys: {', '.join(unknown)}.")
        text = (item.get("text") or "").strip() if isinstance(item.get("text"), str) else ""
        if not text:
            continue
        if len(text) > MAX_KEYWORD_CHARS:
            raise RuleValidationError("That keyword is too long.")
        raw_mode = item.get("mode")
        mode = raw_mode if isinstance(raw_mode, str) and raw_mode in _KEYWORD_MODES else "contains"
        # Deduped case-insensitively, like the trigger form: two keywords
        # differing only in case match the same messages, so the second could
        # never be the one that won.
        fingerprint = f"{text.casefold()}|{mode}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append({"text": text, "mode": mode})
    return out


def _is_member(workspace: Any, user_id: str) -> bool:
    from apps.members.models import WorkspaceMembership

    parsed = _uuid(user_id)
    if parsed is None:
        return False
    # WorkspaceMembership is not a WorkspaceScopedModel, so this is a plain
    # filter on the column rather than .for_workspace() — the same shape
    # apps/inbox/views.py::_membership uses.
    return WorkspaceMembership.objects.filter(workspace=workspace, user_id=parsed).exists()


def _uuid(value: str) -> Any:
    from uuid import UUID

    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [item for item in _list(value) if isinstance(item, str) and item]
