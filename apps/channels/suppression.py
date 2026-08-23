"""Reading and writing the email suppression list (SPEC §6.7).

Three callers, one module: the unsubscribe view records an unsubscribe, the
inbound processor records a bounce or a complaint, and the adapter asks whether
an address is suppressed just before it hands anything to a provider.

Why the answer is *also* pushed onto the identity, in
:func:`suppress_and_opt_out`: an address-keyed list survives the identity being
erased or orphaned (see ``EmailSuppression``'s docstring), but only the adapter
consults it, and the adapter is the last thing in the send path. The
compliance engine — SPEC §19's chokepoint, and the thing every *set-wise*
consumer uses, including a broadcast's eligibility preview — reads
``identity.opted_out_at``. Writing both means the durable record is the mailbox
while the enforcement point stays where SPEC put it, and a re-imported contact
costs exactly one refused send before the chokepoint knows again.

``opted_out_at`` is never assigned here. It has one write site,
``apps.messaging.ingest.apply_opt_out`` (ROADMAP contract 3, asserted over the
AST by ``apps/messaging/tests/test_write_sites.py``), and the door this module
uses is ``apps.messaging.services.record_opt_out``.
"""

import logging
from typing import Any

from django.db import IntegrityError, transaction

from apps.channels.models import EmailSuppression, SuppressionReason
from apps.common.addresses import normalize_email

logger = logging.getLogger(__name__)

__all__ = [
    "is_suppressed",
    "suppress",
    "suppress_and_opt_out",
]

#: Cap on what goes in ``detail``. The column is 200; truncating here keeps a
#: provider's over-long subtype from raising ``DataError`` on the webhook path.
MAX_DETAIL_CHARS = 200


def is_suppressed(workspace_id: Any, address: str) -> bool:
    """Whether this workspace must not mail ``address``.

    An address that does not normalise is not suppressed *and* is not sendable —
    the caller refuses it separately, on the grounds that it is not an address.
    """
    normalized = normalize_email(address)
    if not normalized:
        return False
    return EmailSuppression.objects.for_workspace(workspace_id).filter(address=normalized).exists()


def suppress(
    workspace: Any,
    address: str,
    *,
    reason: str,
    detail: str = "",
    connection: Any = None,
) -> EmailSuppression | None:
    """Record ``address`` as unmailable. Returns the row, or ``None``.

    ``None`` for an address that does not normalise — a bounce notification is
    attacker-shaped input (SECURITY-BASELINE §2) and the ``To`` in one is
    whatever the provider echoed back.

    **The first reason is the one that is kept.** A mailbox that hard-bounced and
    later gets an unsubscribe recorded against it did not stop having hard
    bounced, and moving the row's reason forward would lose why it was suppressed
    in the first place. That is the same argument ``apply_opt_out`` makes for not
    re-stamping ``opted_out_at``.
    """
    normalized = normalize_email(address)
    if not normalized:
        logger.info("Refusing to suppress %r: not an address.", address[:64])
        return None

    # Probe, then create, with the unique index arbitrating a race — the shape
    # `apps.contacts.services.get_or_create_tag` uses on every scoped model.
    # `get_or_create` is not an option: it runs its `get` on the manager's
    # queryset, which `WorkspaceScopedModel` refuses to execute unscoped.
    existing = EmailSuppression.objects.for_workspace(workspace).filter(address=normalized).first()
    if existing is not None:
        # **The first reason is the one that is kept**, so this returns rather
        # than updating. See the docstring.
        return existing
    try:
        # A savepoint, because a losing race raises IntegrityError and would
        # otherwise mark the surrounding transaction unusable — the webhook path
        # writes several of these in one delivery.
        with transaction.atomic():
            row = EmailSuppression.objects.create(
                workspace=workspace,
                address=normalized,
                reason=reason,
                detail=detail[:MAX_DETAIL_CHARS],
                connection=connection,
            )
    except IntegrityError:
        # Two deliveries for the same bounce, concurrently. The other one won,
        # and the requested state exists.
        return EmailSuppression.objects.for_workspace(workspace).filter(address=normalized).first()
    logger.info("Suppressed an address in workspace %s (%s)", getattr(workspace, "pk", workspace), reason)
    return row


def suppress_and_opt_out(
    identity: Any,
    *,
    reason: str,
    detail: str = "",
    connection: Any = None,
) -> None:
    """Suppress the identity's address **and** withdraw consent on the identity.

    The pair, always together — see the module docstring. Called by the
    unsubscribe view, by the bounce processor and by the adapter when it finds a
    suppressed address on an identity that does not know it yet.

    Tolerant of the facade being absent, which is the same allowance
    ``apps.flows.messaging`` makes: a tree where ``apps.messaging`` is not
    installed still records the durable half.
    """
    address = str(getattr(identity, "platform_user_id", "") or "")
    # The instance, not the id: `get_or_create` assigns it straight onto the
    # row's FK, which refuses a bare UUID.
    workspace = getattr(identity, "workspace", None)
    if workspace is not None:
        suppress(workspace, address, reason=reason, detail=detail, connection=connection)
    try:
        from apps.messaging.services import record_opt_out
    except ImportError:  # pragma: no cover - the facade ships in this repo
        logger.warning("Suppressed %s with no messaging facade to record the opt-out on.", identity.pk)
        return
    record_opt_out(identity, source=reason or SuppressionReason.MANUAL.value)
