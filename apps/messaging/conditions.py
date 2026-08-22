"""The ``window`` condition source (ROADMAP contract 8, SPEC §11.4).

``apps.contacts.conditions`` declares six sources and freezes their vocabulary
so ``CONDITION_SCHEMA`` cannot depend on import order. Two of them ship with
``build_q=None`` — a *declared slot*: a filter using one validates and can be
saved, and raises :class:`SourceNotEvaluableError` if evaluated, until the
owning issue supplies the behaviour. ``window`` is this issue's slot; ``sequence``
is #22's.

So this module supplies **behaviour only**. The name, label, key kind and
operators all come back out of the declaration untouched — ``register_source``
refuses a registration that would change them, because issue #6 already embedded
the schema and the flow builder generates its panels from it.

The rule's ``key`` is a platform (``KEY_PLATFORM``), not a UUID, so
``rule.target_id`` is None and the platform is read from ``rule.key`` — which
``_parse_key`` has already checked against ``Platform.values``.
"""

from datetime import datetime
from typing import Any, Protocol

from django.db.models import Exists, OuterRef, Q

from apps.channels.policy import policy_for
from apps.contacts.conditions import (
    KEY_PLATFORM,
    OPS_BY_SOURCE,
    SOURCE_LABELS,
    ConditionSource,
    Rule,
    register_source,
)
from apps.messaging.models import ContactChannelIdentity

__all__ = ["register_window_source"]


class _CompilationContext(Protocol):
    """The two attributes a source needs off the condition engine's context.

    Structural rather than an import of ``conditions._Ctx``: the engine's
    context class is private to that module, and what a source is actually
    entitled to is the workspace being compiled for and the "now" the whole
    filter agrees on. Reaching for the concrete class would couple this module
    to a name its owner is free to change.
    """

    workspace: Any
    now: datetime


#: Repeated from the declaration so ``register_source`` sees an identical
#: dataclass on a second ``ready()`` and short-circuits. Anything else here
#: would be rejected as a vocabulary change.
_OWNER = "issue #8, L3-A"


def _window_q(ctx: _CompilationContext, rule: Rule) -> Q:
    """Contacts we can message on ``rule.key``'s platform right now.

    Three clauses, and the third is why this is not just a date comparison:

    1. **An identity exists** for this platform. A contact whose address we do
       not hold is not "inside" anything.
    2. **There is consent and no opt-out.** The only real use of this source is
       targeting a send, and a filter that looks safe while quietly including
       people the send then refuses is worse than no filter at all (SPEC §19).
       Both halves matter: an opted-out identity is obvious, but an identity
       with no recorded consent at all — captured by import or API — is refused
       by ``can_send`` with ``no_opt_in`` just as firmly, and on a windowless
       platform there is no date predicate left to exclude it. Leaving it in
       handed an operator a count the send then silently shrank.
    3. **The window is open — if the platform has one.** Telegram, SMS and email
       have ``window_hours=None``, so their identities never carry a
       ``window_expires_at`` and a bare date comparison would put *nobody*
       inside. ``has_window()`` is consulted first, exactly as
       ``apps.channels.policy``'s docstring requires, and for those platforms
       "inside" means "reachable".

    ``for_workspace``, not ``filter``: ``WorkspaceScopedQuerySet``'s guard fires
    on *execution*, and a queryset handed to ``Exists()`` is compiled into the
    outer statement rather than executed — so this predicate is the subquery's
    only tenancy check.

    ``outside`` is ``NOT EXISTS``, which makes it true for a contact with no
    identity on that platform at all. That is deliberate and is the same absence
    rule ``has_not`` and ``no_value`` follow: each pair of operators has to
    partition the workspace, or a broadcast built from one half and a
    suppression list built from the other would disagree about somebody.

    ``rule.key`` reaches the ORM as a bound parameter on the ``platform``
    column, and the schema has already checked it against ``Platform.values`` —
    so the enum is the allowlist and no user string becomes part of a lookup
    (SECURITY-BASELINE §7).
    """
    rows = ContactChannelIdentity.objects.for_workspace(ctx.workspace).filter(
        contact=OuterRef("pk"),
        platform=rule.key,
        opt_in=True,
        opted_out_at__isnull=True,
    )
    if policy_for(rule.key).has_window():
        # A NULL window_expires_at — no window ever opened — fails __gt and so
        # reads as outside, which is the direction to fail in.
        rows = rows.filter(window_expires_at__gt=ctx.now)
    inside = Q(Exists(rows))
    return inside if rule.op == "inside" else ~inside


def register_window_source() -> None:
    """Fill the ``window`` slot. Called from ``MessagingConfig.ready()``.

    Idempotent by dataclass equality: ``_window_q`` is a module-level function,
    so a second call builds an identical :class:`ConditionSource` and
    ``register_source`` returns early. ``ready()`` runs twice under some
    autoreload paths and that must not be an error.
    """
    register_source(
        ConditionSource(
            "window",
            SOURCE_LABELS["window"],
            KEY_PLATFORM,
            OPS_BY_SOURCE["window"],
            _window_q,
            _OWNER,
        )
    )
