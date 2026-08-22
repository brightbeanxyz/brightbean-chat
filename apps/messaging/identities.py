"""Resolving "who is this?" from a platform id (issue #8, SPEC §5).

Every inbound event names a person the only way its platform can: a Telegram
chat id, a Meta PSID, a phone number, an email address. This module turns that
into a :class:`~apps.messaging.models.ContactChannelIdentity` and the
:class:`~apps.contacts.models.Contact` behind it, and it is the only place that
decides whether two channels are the same human being.

--------------------------------------------------------------------------
The linking rules, in order
--------------------------------------------------------------------------

1. **An existing identity wins.** ``(connection, platform_user_id)`` is unique,
   so a hit is not a guess — the platform already told us, on a previous event,
   whose thread this is.
2. **Only address-bearing platforms link across channels.** For SMS the
   ``platform_user_id`` *is* a phone number and for email it *is* an address, so
   comparing it to ``contact.phone`` / ``contact.email`` compares like with
   like. A Telegram chat id or a Meta PSID is an opaque per-app number that
   means nothing outside that platform, and matching on it would be matching on
   coincidence.
3. **Exactly one match links. Two or more create a new contact.** This is the
   rule the issue calls out and it is asymmetric on purpose. Failing to link
   leaves two contact rows for one person: visible in the CRM, fixable with
   ``merge_contacts``. Linking wrongly staples two strangers' conversation
   histories together, and nothing in the product can tell afterwards which
   messages belonged to whom.
4. **A normalised address only matches a normalised address.** ``normalize_phone``
   refuses to invent a country code (see :mod:`apps.common.addresses`), so a
   contact whose phone was typed in national format simply does not match and
   rule 3 creates a new contact. Contact rows are compared on both the raw and
   the normalised form so an operator who typed E.164 gets the link they expect.

WhatsApp is a deliberate omission from rule 2. Its ``wa_id`` *is* an E.164
number without the ``+``, which would make it linkable — but the code that knows
that is the adapter's, and the adapter is L5-C's. Adding ``Platform.WHATSAPP``
to :data:`ADDRESS_PLATFORMS` there is the whole change.
"""

import hashlib
import logging
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.addresses import normalize_email, normalize_phone
from apps.common.platforms import Platform
from apps.contacts.models import Contact, ContactStatus
from apps.contacts.services import create_contact
from apps.messaging.models import ContactChannelIdentity, OptInSource

logger = logging.getLogger(__name__)

__all__ = [
    "ADDRESS_PLATFORMS",
    "IdentityResolution",
    "bounded_address",
    "normalized_address_for",
    "resolve_identity",
]

#: Platforms whose ``platform_user_id`` is a real-world address, and which can
#: therefore link to a contact captured on another channel. See rule 2.
ADDRESS_PLATFORMS: dict[str, str] = {
    Platform.SMS.value: "phone",
    Platform.EMAIL.value: "email",
}

#: Longest ``platform_user_id`` the column holds.
MAX_PLATFORM_USER_ID = 200


def bounded_address(value: str) -> str:
    """A storable ``platform_user_id``: NUL-free, and bounded without truncation.

    An over-long id is **hashed**, not cut. Truncation narrows an identity key
    without saying so, and two ids that happen to agree in their first 200
    characters would silently become one person — which on this table means one
    person receiving another's conversation. ``apps.channels.views_webhooks``
    made the same call for ``provider_event_id`` and for the same reason.

    Returns ``""`` for an empty or unusable value, which the caller treats as
    "no identity to resolve" rather than storing an empty id that would collide
    with every other empty one.
    """
    if not isinstance(value, str):
        return ""
    cleaned = value.replace("\x00", "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= MAX_PLATFORM_USER_ID:
        return cleaned
    return f"sha256:{hashlib.sha256(cleaned.encode('utf-8')).hexdigest()}"


class IdentityResolution:
    """The identity, its contact, and whether either was created just now.

    ``created_identity`` is what tells ingest to record consent: an identity
    that already existed has an ``opt_in_at`` from whenever it was first seen,
    and overwriting it would destroy the audit trail it exists to be.
    """

    __slots__ = ("contact", "created_contact", "created_identity", "identity")

    def __init__(
        self,
        identity: ContactChannelIdentity,
        contact: Contact,
        *,
        created_identity: bool,
        created_contact: bool,
    ) -> None:
        self.identity = identity
        self.contact = contact
        self.created_identity = created_identity
        self.created_contact = created_contact


def normalized_address_for(platform: str, platform_user_id: str) -> tuple[str, str]:
    """``(contact_field, normalised_value)`` for an address-bearing platform.

    ``("", "")`` when the platform's ids are opaque, or when the value will not
    normalise — both of which mean "do not attempt to link".
    """
    field = ADDRESS_PLATFORMS.get(platform, "")
    if not field:
        return "", ""
    normalized = normalize_phone(platform_user_id) if field == "phone" else normalize_email(platform_user_id)
    return (field, normalized) if normalized else ("", "")


def _matching_contact(workspace_id: Any, field: str, raw: str, normalized: str) -> Contact | None:
    """The one active contact holding this address, or None.

    None covers both "nobody" and "more than one" — rule 3 treats them
    identically, and the caller has no use for the difference beyond the log
    line emitted here.
    """
    candidates = {value for value in (raw, normalized) if value}
    # ``field`` is a value of ADDRESS_PLATFORMS — a module constant, never a
    # platform-supplied string — so no attacker-controlled text reaches a query
    # kwarg here (SECURITY-BASELINE §7). The *values* are bound parameters.
    matches = list(
        Contact.objects.for_workspace(workspace_id)
        .filter(status=ContactStatus.ACTIVE, **{f"{field}__in": candidates})
        .order_by("created_at")[:2]
    )
    if len(matches) == 1:
        return matches[0]
    if matches:
        # Not an error and not silent: an operator looking at two contacts with
        # one phone number should be able to find out why they were not merged.
        logger.info(
            "Ambiguous %s match in workspace %s; creating a new contact rather than guessing.",
            field,
            workspace_id,
        )
    return None


def resolve_identity(connection: Any, platform_user_id: str, *, occurred_at: Any = None) -> IdentityResolution:
    """The identity for ``platform_user_id`` on ``connection``, creating as needed.

    Creation is racy by nature — two webhook deliveries for a contact's first two
    messages can arrive at once — so the unique index is the arbitrator: the
    insert is attempted inside a savepoint and a conflict re-reads the winner.
    A check-then-act would leave one of the two events attached to a contact row
    that is about to lose its identity.
    """
    platform = connection.platform

    existing = (
        ContactChannelIdentity.objects.for_workspace(connection.workspace_id)
        .filter(channel_connection=connection, platform_user_id=platform_user_id)
        .select_related("contact")
        .first()
    )
    if existing is not None:
        return IdentityResolution(existing, existing.contact, created_identity=False, created_contact=False)

    field, normalized = normalized_address_for(platform, platform_user_id)
    contact = None
    created_contact = False
    if field:
        contact = _matching_contact(connection.workspace_id, field, platform_user_id, normalized)

    if contact is None:
        # source="inbound" is already a member of contacts.services.CONTACT_SOURCES.
        # Going through the service rather than Contact.objects.create is what
        # makes contact.created fire exactly once, from the app that owns it.
        address_kwargs: dict[str, str] = {field: normalized} if field else {}
        contact = create_contact(
            connection.workspace,
            last_interaction_at=occurred_at or timezone.now(),
            source="inbound",
            **address_kwargs,
        )
        created_contact = True
    elif field and not getattr(contact, field):  # pragma: no cover - defensive
        # Matched on the raw value while the normalised column was empty. Cannot
        # happen today (the match is *on* that column) and is here so a future
        # matcher that widens the candidate set cannot silently skip the fill.
        setattr(contact, field, normalized)
        contact.save(update_fields=[field, "updated_at"])

    identity = ContactChannelIdentity(
        contact=contact,
        channel_connection=connection,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    try:
        with transaction.atomic():
            identity.save()
    except IntegrityError:
        # Another delivery won the race. Its row is authoritative; the contact
        # this call may have just created is left behind rather than deleted —
        # a stray empty contact is recoverable, and a delete here would race the
        # winner's own writes to it.
        winner = (
            ContactChannelIdentity.objects.for_workspace(connection.workspace_id)
            .filter(channel_connection=connection, platform_user_id=platform_user_id)
            .select_related("contact")
            .get()
        )
        return IdentityResolution(winner, winner.contact, created_identity=False, created_contact=False)

    return IdentityResolution(identity, contact, created_identity=True, created_contact=created_contact)


def record_consent(
    identity: ContactChannelIdentity,
    *,
    source: str,
    opt_in: bool = True,
    now: Any = None,
) -> list[str]:
    """Stamp the consent audit onto ``identity``. Returns the changed field names.

    Only ever *adds* consent. ``opted_out_at`` is untouched here: a contact who
    sent STOP and then types again has not re-subscribed, and treating a message
    as re-consent is exactly the bug that makes an opt-out look optional.
    Re-subscription is an explicit keyword (L5-D's ``hard_optout`` hook) that
    reaches the identity through a different door.

    ``opt_in_at`` is written once, when consent is first recorded. Refreshing it
    on every inbound message would replace the moment consent was given with the
    moment it was last exercised, which is the fact the audit is not asking for.
    """
    if not opt_in or identity.opted_out_at is not None:
        return []
    changed: list[str] = []
    if not identity.opt_in:
        identity.opt_in = True
        changed.append("opt_in")
    if identity.opt_in_at is None:
        identity.opt_in_at = now or timezone.now()
        changed.append("opt_in_at")
    if not identity.opt_in_source:
        identity.opt_in_source = source or OptInSource.MESSAGE_IN
        changed.append("opt_in_source")
    return changed
