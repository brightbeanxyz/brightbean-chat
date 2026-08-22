"""The compliance engine — SPEC §8's single chokepoint.

    ``can_send(identity, source, outbound) -> Allowed | NeedsTemplate(reason) |
    NeedsTag(allowed_tags) | Blocked(reason)``. Called by the send pipeline for
    every outbound message, no exceptions.

SPEC §19 puts opt-out enforcement here "and not in flows, so it cannot be
bypassed", which is what makes this module worth its length.

--------------------------------------------------------------------------
No per-platform branches, and how that is kept true
--------------------------------------------------------------------------

Not one platform name appears below. Every decision reads a field of
``apps.channels.policy.PlatformPolicy`` — ``window_hours``, ``outside_window``,
``human_agent_days``, ``broadcast_allowed`` — which is contract 4's promise that
a Layer-5 platform costs "one module and one registry line". An adapter adds a
policy row; it never patches this file.

``has_window()`` is consulted **before** ``outside_window``, which
``apps.channels.policy``'s own docstring says is load-bearing: the literal is
populated for Telegram, SMS and email even though it is unreachable there, so a
consumer that read it without checking would refuse every windowless send.

--------------------------------------------------------------------------
One rule list, two evaluators
--------------------------------------------------------------------------

SPEC §13.2 needs the same rules applied *set-wise* before a broadcast fans out,
and two implementations of a compliance rule is two chances to disagree — with
the failure mode being a broadcast whose preview count includes people the send
then refuses, or worse, does not.

So :func:`_rules` returns an ordered list in which each rule carries **both**
spellings of the same predicate: a ``Q`` for the database and a callable for
Python. ``can_send`` walks it; :func:`annotate_eligibility` compiles the same
list — the same objects, built once — into a ``Case``/``When``, which evaluates
top-down in exactly the same order. Ordering, the broadcast short-circuit, the
window short-circuit and the outside-window answer are therefore literally
shared, and the only thing that can drift is one rule's two spellings.
``test_compliance_setwise.py`` closes that by asserting the two agree row by row
for every platform and source.

--------------------------------------------------------------------------
The tag is ours, not the caller's
--------------------------------------------------------------------------

:meth:`Allowed.apply` **replaces** ``outbound.tag`` rather than passing the
caller's through. That is a security property: SPEC §22 says the human-agent
allowance is "available only to inbox sends, never automation, hard-coded", and
without this an automation node could set ``tag="HUMAN_AGENT"`` and buy itself
the seven-day escape.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Case, CharField, Q, QuerySet, Value, When
from django.utils import timezone

# The module, not the name: apps.channels.policy exports a NeedsTag too, and it
# is a different thing — policy DATA (which tags a platform accepts, and Meta's
# allowed-use text) rather than a DECISION. SPEC §8 and contract 1 both write
# this module's result type out as NeedsTag, so the public name has to stay, and
# the policy class is spelled out in full at every use instead.
from apps.channels import policy as channel_policy
from apps.channels.events import OutboundMessage
from apps.messaging.codes import Denial, Grant
from apps.messaging.models import ContactChannelIdentity, MessageSource

__all__ = [
    "ALLOWED_CODES",
    "Allowed",
    "Blocked",
    "Decision",
    "HUMAN_AGENT_TAG",
    "NeedsTag",
    "NeedsTemplate",
    "annotate_eligibility",
    "can_send",
    "eligible",
]

#: Meta's human-agent message tag. Hard-coded rather than policy data because
#: SPEC §22 hard-codes it; whether a platform *offers* it is policy data
#: (``human_agent_days``), and that is the part this module branches on.
HUMAN_AGENT_TAG = "HUMAN_AGENT"

#: Sources the human-agent allowance is available to. A frozenset of one, so the
#: rule reads as the policy it is rather than an equality check that could grow
#: a second source by accident.
AGENT_SOURCES = frozenset({MessageSource.AGENT.value})

#: Sources ``PlatformPolicy.broadcast_allowed`` gates.
BROADCAST_SOURCES = frozenset({MessageSource.BROADCAST.value})

#: The default annotation name for the set-wise variant.
DECISION_FIELD = "send_decision"


@dataclass(frozen=True)
class Allowed:
    """Send it. ``code`` is a :class:`~apps.messaging.codes.Grant`."""

    code: str
    tag: str | None = None
    template_ref: str | None = None

    def apply(self, outbound: OutboundMessage) -> OutboundMessage:
        """The message as it may actually go on the wire.

        The tag is **replaced**, never merged: see the module docstring. The
        template reference is the caller's, because that one they had to supply
        for the decision to come back allowed at all.
        """
        return replace(outbound, tag=self.tag, template_ref=self.template_ref or outbound.template_ref)


@dataclass(frozen=True)
class Blocked:
    """No, and there is nothing the caller can add to change that."""

    code: str


@dataclass(frozen=True)
class NeedsTemplate:
    """No, but an approved template would be allowed."""

    code: str = Denial.NEEDS_TEMPLATE.value


@dataclass(frozen=True)
class NeedsTag:
    """No, but one of ``allowed_tags`` would be allowed.

    ``allowed_use_text`` is Meta's own description of what those tags may be
    used for, carried through from the policy table because SPEC §6.4 requires
    the broadcast composer to display it verbatim.
    """

    allowed_tags: tuple[str, ...]
    allowed_use_text: str
    code: str = Denial.NEEDS_TAG.value


type Decision = Allowed | NeedsTemplate | NeedsTag | Blocked

#: Every code that means "allowed". The set-wise variant filters on it.
ALLOWED_CODES = frozenset(grant.value for grant in Grant)


@dataclass(frozen=True)
class _Rule:
    """One predicate in two spellings, so the two evaluators cannot drift."""

    code: str
    q: Q
    test: Callable[[Any, datetime], bool]


def _terminal(code: str) -> _Rule:
    """The last rule, which always matches. Whatever is left gets this answer."""
    return _Rule(code, Q(), lambda identity, now: True)


def _rules(
    policy: channel_policy.PlatformPolicy,
    source: str,
    outbound: OutboundMessage,
    now: datetime,
) -> tuple[_Rule, ...]:
    """SPEC §8's rules, in order, for one platform policy and one source."""
    rules = [
        # First, unconditionally, on every platform, for every source. SPEC §8's
        # first line and SPEC §19's reason this module exists.
        _Rule(
            Denial.OPTED_OUT.value,
            Q(opted_out_at__isnull=False),
            lambda identity, now: identity.opted_out_at is not None,
        ),
        # An address we hold with no record of permission is not sendable. SPEC
        # §8's list names only opted_out_at, and §6.2 separately says to "enforce
        # opt_in on identity" — this generalises that to every platform rather
        # than making it a Telegram branch, and it is the direction to fail in:
        # an inbound message sets opt_in, so this only bites addresses captured
        # by import or API without consent, which is exactly the case §11.8's
        # audit exists for.
        _Rule(Denial.NO_OPT_IN.value, Q(opt_in=False), lambda identity, now: not identity.opt_in),
        # A pending identity (contract 1) has no connection to send through yet.
        # The facade upgrades it lazily; until then there is nothing to call.
        _Rule(
            Denial.NO_CONNECTION.value,
            Q(channel_connection__isnull=True),
            lambda identity, now: identity.channel_connection_id is None,
        ),
    ]

    if source in BROADCAST_SOURCES and not policy.broadcast_allowed:
        # Before the window is even consulted: an open window does not make a
        # broadcast permissible on a platform that forbids them.
        return (*rules, _terminal(Denial.BROADCAST_NOT_ALLOWED.value))

    if not policy.has_window():
        # outside_window is unreachable here and deliberately never read.
        return (*rules, _terminal(Grant.NO_WINDOW.value))

    rules.append(
        _Rule(
            Grant.IN_WINDOW.value,
            Q(window_expires_at__gt=now),
            # A NULL window_expires_at — never opened — reads as closed, which
            # is the direction to fail in.
            lambda identity, now: identity.window_expires_at is not None and identity.window_expires_at > now,
        )
    )

    if source in AGENT_SOURCES and policy.human_agent_days is not None:
        cutoff = now - timedelta(days=policy.human_agent_days)

        def within_human_agent_window(identity: Any, _now: datetime, cutoff: datetime = cutoff) -> bool:
            return identity.last_inbound_at is not None and identity.last_inbound_at >= cutoff

        rules.append(_Rule(Grant.HUMAN_AGENT.value, Q(last_inbound_at__gte=cutoff), within_human_agent_window))

    return (*rules, _terminal(_outside_window_code(policy, outbound)))


def _outside_window_code(policy: channel_policy.PlatformPolicy, outbound: OutboundMessage) -> str:
    """What happens to a send once the window has closed and no escape applied."""
    outside = policy.outside_window
    if isinstance(outside, channel_policy.NeedsTag):
        # SPEC §8: "unless a tag already set and valid". A tag outside the
        # allowed tuple is refused rather than quietly passed through — Meta
        # disables pages over exactly that.
        return Grant.TAG_SUPPLIED.value if outbound.tag in outside.tags else Denial.NEEDS_TAG.value
    if outside == "needs_template":
        return Grant.TEMPLATE_SUPPLIED.value if outbound.template_ref else Denial.NEEDS_TEMPLATE.value
    return Denial.OUTSIDE_WINDOW.value


def can_send(
    identity: ContactChannelIdentity,
    source: str,
    outbound: OutboundMessage,
    *,
    now: datetime | None = None,
) -> Decision:
    """May this message go out? SPEC §8, and the only place that decides.

    Never raises for a compliance reason — contract 1 requires a denial to come
    back as a value the caller turns into a failed message row, because a raise
    would kill the flow that a mere refused send should not.
    """
    now = now or timezone.now()
    policy = channel_policy.policy_for(identity.platform)
    for rule in _rules(policy, source, outbound, now):
        if rule.test(identity, now):
            return _decision(rule.code, policy, outbound)
    raise AssertionError("the terminal rule always matches")  # pragma: no cover


def _decision(code: str, policy: channel_policy.PlatformPolicy, outbound: OutboundMessage) -> Decision:
    if code == Grant.HUMAN_AGENT:
        return Allowed(code, tag=HUMAN_AGENT_TAG)
    if code == Grant.TAG_SUPPLIED:
        return Allowed(code, tag=outbound.tag)
    if code == Grant.TEMPLATE_SUPPLIED:
        return Allowed(code, template_ref=outbound.template_ref)
    if code in ALLOWED_CODES:
        return Allowed(code)
    if code == Denial.NEEDS_TAG:
        outside = policy.outside_window
        if not isinstance(outside, channel_policy.NeedsTag):
            # Unreachable: only _outside_window_code emits NEEDS_TAG, and only
            # for a NeedsTag policy. Checked rather than asserted because
            # ``python -O`` strips an assert, and the line after it would then
            # raise AttributeError from inside the send chokepoint instead of
            # failing the way this module documents.
            raise TypeError(f"NEEDS_TAG from a {type(outside).__name__} policy")
        return NeedsTag(allowed_tags=outside.tags, allowed_use_text=outside.allowed_use_text)
    if code == Denial.NEEDS_TEMPLATE:
        return NeedsTemplate()
    return Blocked(code)


def annotate_eligibility(
    identities: QuerySet[ContactChannelIdentity],
    *,
    connection: Any,
    source: str,
    outbound: OutboundMessage,
    now: datetime | None = None,
    field: str = DECISION_FIELD,
) -> QuerySet[ContactChannelIdentity]:
    """Bulk-annotate each identity with its decision code (SPEC §13.2).

    Narrowed to one ``connection`` because that is what makes deriving a single
    ``PlatformPolicy`` sound — a broadcast targets exactly one connection (SPEC
    §13.1) — and because the policy is what the whole rule list is built from.

    The queryset is the caller's and stays the caller's: this never scopes it,
    so ``WorkspaceScopedQuerySet``'s guard still fires if a caller forgot
    ``for_workspace()``. Compiled through the ORM only, with no user string
    reaching a lookup (SECURITY-BASELINE §7).

    ``skipped_window`` and its siblings for SPEC §13.2's counters fall out of
    ``.values(field).annotate(Count("id"))``.
    """
    now = now or timezone.now()
    rules = _rules(channel_policy.policy_for(connection.platform), source, outbound, now)
    *guards, terminal = rules
    # This connection's identities, plus the pending records for its platform.
    # Narrowing to the connection alone is what makes deriving a single policy
    # sound, but it also dropped pending identities out of the result entirely —
    # so the NO_CONNECTION rule was unreachable here while reachable in
    # can_send(), and a broadcast preview silently omitted those people instead
    # of counting them under a skip reason (SPEC §13.2).
    in_scope = Q(channel_connection=connection) | Q(channel_connection__isnull=True, platform=connection.platform)
    return identities.filter(in_scope).annotate(
        **{
            field: Case(
                *[When(rule.q, then=Value(rule.code)) for rule in guards],
                default=Value(terminal.code),
                output_field=CharField(),
            )
        }
    )


def eligible(
    identities: QuerySet[ContactChannelIdentity],
    **kwargs: Any,
) -> QuerySet[ContactChannelIdentity]:
    """Just the identities a send would be allowed to. Exported for L6-B."""
    field = kwargs.get("field", DECISION_FIELD)
    return annotate_eligibility(identities, **kwargs).filter(**{f"{field}__in": sorted(ALLOWED_CODES)})
