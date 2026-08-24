"""The ``sequence`` condition source (ROADMAP contract 8, SPEC §11.4).

``apps.contacts.conditions`` declares six sources and freezes their vocabulary
so ``CONDITION_SCHEMA`` cannot depend on import order. Two of them shipped with
``build_q=None`` — a *declared slot*: a filter using one validates and can be
saved, and raises ``SourceNotEvaluableError`` if evaluated, until the owning
issue supplies the behaviour. ``window`` was L3-A's; ``sequence`` is this one's,
declared with the note ``"issue #22, L6-A"``.

So this module supplies **behaviour only**. The name, label, key kind and
operators all come back out of the declaration untouched — ``register_source``
refuses a registration that would change them, because issue #6 already embedded
the schema and the flow builder generates its panels from it.

**Set-wise, in one query.** The predicate is an ``EXISTS`` over enrollments
correlated to the outer contact, so ``queryset()`` stays a single statement
whether it is answering a segment count, a broadcast audience or one contact's
condition node. A Python loop over contacts here would be a second
implementation of the operator semantics as well as a scaling problem.
"""

from datetime import datetime
from typing import Any, Protocol

from django.db.models import Exists, OuterRef, Q

from apps.campaigns.models import EnrollmentStatus, SequenceEnrollment
from apps.contacts.conditions import (
    KEY_UUID,
    OPS_BY_SOURCE,
    SOURCE_LABELS,
    ConditionSource,
    Rule,
    register_source,
)

__all__ = ["register_sequence_source"]


class _CompilationContext(Protocol):
    """The two attributes a source needs off the condition engine's context.

    Structural rather than an import of ``conditions._Ctx``: the engine's context
    class is private to that module, and what a source is entitled to is the
    workspace being compiled for and the "now" the whole filter agrees on.
    """

    workspace: Any
    now: datetime


#: Repeated from the declaration so ``register_source`` sees an identical
#: dataclass on a second ``ready()`` and short-circuits. Anything else here would
#: be rejected as a vocabulary change.
_OWNER = "issue #22, L6-A"


def _sequence_q(ctx: _CompilationContext, rule: Rule) -> Q:
    """Contacts currently walking ``rule.key``'s sequence.

    "Subscribed" means an **active** enrollment. A contact who finished the
    campaign or was unsubscribed from it is not subscribed to it — the row stays
    for history, and treating history as membership would make "not subscribed to
    onboarding" exclude everybody who ever completed it, which is the opposite of
    what an operator building a follow-up campaign means.

    ``for_workspace``, not ``filter``: ``WorkspaceScopedQuerySet``'s guard fires
    on *execution*, and a queryset handed to ``Exists()`` is compiled into the
    outer statement rather than executed — so this predicate is the subquery's
    only tenancy check.

    ``not`` is ``NOT EXISTS``, so it is true for a contact with no enrollment row
    at all. That is deliberate and is the same absence rule ``has_not`` and
    ``no_value`` follow: each pair of operators has to partition the workspace,
    or a campaign built from one half and a suppression list built from the other
    would disagree about somebody.

    ``rule.target_id`` is a UUID the engine has already parsed, and it reaches
    the ORM as a bound parameter — no user string becomes part of a lookup
    (SECURITY-BASELINE §7). A sequence id from another workspace simply matches
    nothing, because the subquery is scoped.
    """
    rows = SequenceEnrollment.objects.for_workspace(ctx.workspace).filter(
        contact=OuterRef("pk"),
        sequence_id=rule.target_id,
        status=EnrollmentStatus.ACTIVE,
    )
    subscribed = Q(Exists(rows))
    return subscribed if rule.op == "subscribed" else ~subscribed


def register_sequence_source() -> None:
    """Fill the ``sequence`` slot. Called from ``CampaignsConfig.ready()``.

    Idempotent by dataclass equality: ``_sequence_q`` is a module-level function,
    so a second call builds an identical :class:`ConditionSource` and
    ``register_source`` returns early. ``ready()`` runs twice under some
    autoreload paths and that must not be an error.
    """
    register_source(
        ConditionSource(
            "sequence",
            SOURCE_LABELS["sequence"],
            KEY_UUID,
            OPS_BY_SOURCE["sequence"],
            _sequence_q,
            _OWNER,
        )
    )
