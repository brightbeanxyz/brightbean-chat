"""Who a broadcast goes to, and who it does not (SPEC §13.2).

Two reserved seams meet here and nothing else does:

    apps.contacts.conditions.queryset(workspace, filter_json)   # contract 8
    apps.messaging.compliance.annotate_eligibility(...)         # "Exported for L6-B"

Targeting is the same engine saved segments use, so **an audience and a segment
agree by construction**. Eligibility is the same rule list ``can_send`` walks,
in the same order, because ``compliance._rules`` returns each predicate in two
spellings and both evaluators compile the one list — which is what stops a
preview from counting people the send then refuses.

Nothing here re-derives a messaging window, an opt-out or a template
requirement. If you find yourself reaching for ``window_expires_at`` in this
file, the answer you want is a ``send_decision`` code from the annotation.

--------------------------------------------------------------------------
One send per contact, not per identity
--------------------------------------------------------------------------

``annotate_eligibility`` narrows to "this connection's identities, plus the
pending records for its platform" — deliberately, so a contact whose address was
captured before the connection existed is counted rather than silently dropped.
The consequence is that one contact can match more than once: a real row on the
connection and a leftover pending row for the platform.

SPEC §13.2 inserts "one ``broadcast_send`` action per contact", so this module
collapses that. :func:`iter_candidates` keeps the **best** row per contact —
eligible first, then the connection-bound one, then by id so it is deterministic
— and a contact matching no identity at all is reported under ``no_identity``
rather than vanishing from the totals. Counting identity rows instead would
report an audience larger than the number of messages sent, and the acceptance
criterion is that those reconcile.

--------------------------------------------------------------------------
The preview is advisory; the recipient rows are the record
--------------------------------------------------------------------------

:func:`preview` is set-wise — three aggregate queries whatever the audience size,
because a ten-thousand-contact count must not walk ten thousand objects behind a
composer keystroke. ``eligible`` is exact. The per-reason skip counts are exact
except in one bounded case: a contact holding **two** addresses on the same
connection with two different denials is counted under both, so the reasons can
sum to slightly more than ``total - eligible``. The authoritative per-reason
numbers are the ``BroadcastRecipient`` rows fanout writes, where each contact has
exactly one row by unique constraint.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Case, Count, IntegerField, QuerySet, Value, When

from apps.channels.events import OutboundMessage
from apps.channels.suppression import is_suppressed
from apps.common.addresses import normalize_email
from apps.contacts import conditions
from apps.contacts.models import Contact
from apps.messaging.codes import Denial
from apps.messaging.compliance import ALLOWED_CODES, DECISION_FIELD, annotate_eligibility
from apps.messaging.models import ContactChannelIdentity, MessageSource

logger = logging.getLogger(__name__)

__all__ = [
    "AudiencePreview",
    "Candidate",
    "PREVIEW_SAMPLE",
    "SOURCE",
    "iter_candidates",
    "preview",
    "suppressed",
    "probe_for",
    "target_queryset",
]

#: How many named examples the composer's preview carries per skip reason. A
#: reason with a count and no example is a number an operator cannot act on; a
#: reason with ten thousand examples is a page nobody can read.
PREVIEW_SAMPLE = 5

#: The source every question this module asks is asked as. Fixed rather than a
#: parameter: a preview computed as ``automation`` would answer a different
#: question from the one the send will ask, which is the exact drift
#: ``compliance``'s "one rule list, two evaluators" design exists to prevent.
SOURCE = MessageSource.BROADCAST.value

#: Sort key that puts an eligible candidate ahead of a refused one.
_ELIGIBLE_FIRST = Case(
    When(**{f"{DECISION_FIELD}__in": sorted(ALLOWED_CODES)}, then=Value(0)),
    default=Value(1),
    output_field=IntegerField(),
)


@dataclass(frozen=True)
class Candidate:
    """One contact, the identity a send would use, and the verdict on it."""

    contact_id: Any
    identity_id: Any
    decision: str
    #: The identity's ``platform_user_id`` — an address only where the platform's
    #: addresses are mailboxes. Carried so :func:`suppressed` can be asked without
    #: a second query per contact.
    address: str = ""

    @property
    def is_eligible(self) -> bool:
        return self.decision in ALLOWED_CODES


@dataclass(frozen=True)
class AudiencePreview:
    """What the composer shows before anybody presses send."""

    #: Everyone the filter matches, whatever compliance then says.
    total: int = 0
    #: How many of them a send would actually be allowed to. Exact.
    eligible: int = 0
    #: Denial code -> count. Keys are :class:`apps.messaging.codes.Denial`
    #: values, so the copy comes from ``codes.describe`` at render time rather
    #: than being written here.
    skipped: dict[str, int] = field(default_factory=dict)
    #: Denial code -> a few contact names, for the "why?" panel.
    samples: dict[str, list[str]] = field(default_factory=dict)

    @property
    def skipped_total(self) -> int:
        """Everyone the filter matched who would not receive it."""
        return self.total - self.eligible

    def needs(self, code: str) -> int:
        """How many people are blocked by one specific denial.

        The composer's Messenger and WhatsApp gates are both this question —
        ``needs(Denial.NEEDS_TAG)`` and ``needs(Denial.NEEDS_TEMPLATE)`` — asked
        of the same annotation the preview came from, so a gate and the number
        printed beside it cannot disagree.
        """
        return self.skipped.get(code, 0)


def probe_for(broadcast: Any) -> OutboundMessage:
    """The message shape compliance needs in order to answer at all.

    ``can_send`` reads exactly two fields of the outbound message — ``tag`` and
    ``template_ref`` — because they are what SPEC §8's outside-window escapes
    turn on. Blocks and buttons take no part in a compliance decision, so the
    probe carries none: rendering the real message per contact just to ask
    whether it may be sent would be ten thousand renders for one boolean.
    """
    template = broadcast.whatsapp_template
    return OutboundMessage(
        tag=broadcast.message_tag or None,
        template_ref=template.reference if template is not None else None,
    )


def target_queryset(workspace: Any, filter_json: Any) -> QuerySet[Contact]:
    """The contacts a filter document matches — contract 8, and nothing else.

    ``conditions.queryset`` already restricts to active contacts, so a
    soft-deleted person can never enter a send path through here.
    """
    return conditions.queryset(workspace, filter_json)


def _annotated(broadcast: Any, contacts: QuerySet[Contact]) -> QuerySet[ContactChannelIdentity]:
    """Every candidate identity for this audience, carrying its verdict."""
    identities = ContactChannelIdentity.objects.for_workspace(broadcast.workspace_id).filter(
        contact__in=contacts.values("pk")
    )
    return annotate_eligibility(
        identities,
        connection=broadcast.channel_connection,
        source=SOURCE,
        outbound=probe_for(broadcast),
    )


def iter_candidates(broadcast: Any, *, after: Any = None, limit: int | None = None) -> Iterator[Candidate]:
    """One :class:`Candidate` per contact, in contact-id order, resumable.

    ``after`` is the last contact id a previous chunk saw, which is what lets
    fanout walk a ten-thousand-contact audience five hundred at a time across
    several worker cycles without an ``OFFSET`` that shifts under inserts.

    The per-contact pick is done here in Python rather than with ``DISTINCT ON``
    because a chunk is five hundred rows, the ordering does the work, and this is
    also the only place that can notice a contact with **no** identity at all —
    which the identity join cannot produce a row for and which SPEC §13.2 still
    wants counted.
    """
    compiled = _compile(broadcast)
    contacts = target_queryset(broadcast.workspace_id, compiled)
    if after is not None:
        contacts = contacts.filter(pk__gt=after)
    # values_list before the slice: a sliced queryset refuses further filtering,
    # and taking the ids first keeps the chunk boundary on the same ordering the
    # cursor is expressed in.
    ids = contacts.order_by("pk").values_list("pk", flat=True)
    contact_ids = list(ids[:limit] if limit is not None else ids)
    if not contact_ids:
        return

    rows = (
        _annotated(broadcast, Contact.objects.for_workspace(broadcast.workspace_id).filter(pk__in=contact_ids))
        .annotate(_rank=_ELIGIBLE_FIRST)
        # Eligible first, then the connection-bound row (NULL sorts last
        # ascending in Postgres, and NULL is exactly the pending record), then
        # by id so two addresses on one connection resolve the same way twice.
        .order_by("contact_id", "_rank", "channel_connection_id", "pk")
        .values_list("contact_id", "pk", DECISION_FIELD, "platform_user_id")
    )

    best: dict[Any, tuple[Any, str, str]] = {}
    for contact_id, identity_id, decision, address in rows:
        best.setdefault(contact_id, (identity_id, str(decision), str(address or "")))

    for contact_id in contact_ids:
        found = best.get(contact_id)
        if found is None:
            # No row on this connection and no pending record for the platform.
            # A real skip with a real reason, not an absence.
            yield Candidate(contact_id=contact_id, identity_id=None, decision=Denial.NO_IDENTITY.value)
            continue
        identity_id, decision, address = found
        if decision in ALLOWED_CODES and suppressed(broadcast.workspace_id, address):
            decision = Denial.OPTED_OUT.value
        yield Candidate(contact_id=contact_id, identity_id=identity_id, decision=decision, address=address)


def suppressed(workspace_id: Any, address: str) -> bool:
    """Whether the email suppression list refuses this address (SPEC §6.7).

    Asked **per candidate**, in chunks of five hundred, rather than set-wise —
    and only for an address that normalises as a mailbox, so a Telegram or SMS
    broadcast costs no query at all. ``normalize_email`` is the same function
    ``is_suppressed`` uses, so the two cannot disagree about what an address is.

    Why it is asked at all, given that compliance already refuses an opted-out
    identity: ``suppression.suppress_and_opt_out`` writes both the address-keyed
    list *and* ``opted_out_at``, so the two normally agree. They come apart in
    exactly one case, and that case is the one the list exists for — an address
    whose identity was erased and re-imported. ``apps/channels/suppression.py``
    names the consequence: "a re-imported contact costs exactly one refused send
    before the chokepoint knows again". Checking here turns that refused send
    into a counted skip, which is the difference between an operator seeing a
    failure and seeing a reason.

    Deliberately **not** in :func:`preview`, which is aggregate-only and must
    stay three queries whatever the audience size. The preview reports the
    chokepoint's answer; fanout reports the chokepoint's answer plus this.
    """
    if not normalize_email(address):
        return False
    return is_suppressed(workspace_id, address)


def preview(broadcast: Any) -> AudiencePreview:
    """The composer's live count. Set-wise, whatever the audience size.

    The filter document is compiled **once** and the compiled form reused.
    ``conditions.validate`` resolves every tag, field and segment id a filter
    names against the database, and ``conditions.queryset`` accepts a
    ``CompiledFilter`` precisely so a caller that asks twice does not pay twice —
    which matters here, because the composer fires this on every keystroke.
    """
    compiled = _compile(broadcast)
    contacts = target_queryset(broadcast.workspace_id, compiled)
    total = contacts.count()
    if not total:
        return AudiencePreview()

    annotated = _annotated(broadcast, contacts)
    eligible_contacts = annotated.filter(**{f"{DECISION_FIELD}__in": sorted(ALLOWED_CODES)}).values("contact_id")
    eligible = eligible_contacts.distinct().count()

    skipped: dict[str, int] = {}
    for row in (
        annotated.exclude(contact_id__in=eligible_contacts)
        .values(DECISION_FIELD)
        .annotate(n=Count("contact_id", distinct=True))
    ):
        skipped[str(row[DECISION_FIELD])] = int(row["n"])

    # Contacts the identity join produced no row for at all. A subtraction rather
    # than a second scan, and floored at zero because the bounded double-count
    # the module docstring describes can otherwise push it negative.
    unaccounted = total - eligible - sum(skipped.values())
    if unaccounted > 0:
        skipped[Denial.NO_IDENTITY.value] = skipped.get(Denial.NO_IDENTITY.value, 0) + unaccounted

    return AudiencePreview(
        total=total, eligible=eligible, skipped=skipped, samples=_samples(broadcast, skipped, compiled)
    )


def _samples(broadcast: Any, skipped: dict[str, int], compiled: Any) -> dict[str, list[str]]:
    """A few names per skip reason, so "why?" has an answer a person recognises.

    One small query **per reason**, rather than one slice shared across all of
    them. A shared slice is drawn in contact order, so a reason whose contacts
    sort late gets nothing when another dominates the head: an audience of five
    thousand opted-out contacts and a hundred needing a tag would show the tag
    reason as a bare number, which is precisely the one an operator has to look
    at because it is the one blocking the send.

    The reason set is small and bounded by the compliance vocabulary, so "one
    query per reason" is a handful of ``LIMIT 5`` reads and not a fan-out.
    """
    wanted = sorted(code for code in skipped if code != Denial.NO_IDENTITY.value)
    if not wanted:
        return {}

    contacts = target_queryset(broadcast.workspace_id, compiled)
    annotated = _annotated(broadcast, contacts)

    samples: dict[str, list[str]] = {}
    for code in wanted:
        rows = (
            annotated.filter(**{DECISION_FIELD: code})
            .order_by("contact_id")
            .values_list("contact__first_name", "contact__last_name", "contact__email")[:PREVIEW_SAMPLE]
        )
        # Never HTML — these are contact-authored strings on the attacker-content
        # path (SECURITY-BASELINE §2) and the template escapes them like any
        # other value.
        names = [
            " ".join(part for part in (first, last) if part) or email or "Unnamed contact"
            for first, last, email in rows
        ]
        if names:
            samples[code] = names
    return samples


def _compile(broadcast: Any) -> Any:
    """This broadcast's filter, parsed and resolved once.

    ``conditions.validate`` raises ``ConditionError`` for a document that no
    longer compiles — a segment whose tag somebody deleted, say. Callers here are
    previews and fanout, both of which already treat that as the operator's
    problem rather than a crash, so it propagates.
    """
    return conditions.validate(broadcast.workspace_id, broadcast.target_filter_json)
