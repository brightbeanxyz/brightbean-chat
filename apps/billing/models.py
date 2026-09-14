"""The three tables billing needs, and the argument for what is *not* here.

Two of them are organization-level and one is tenant data:

``BillingCustomer``
    One row per organization, created the first time somebody starts checkout.
    The link between an organization and its Stripe objects, and the only thing
    :mod:`apps.billing.entitlements` reads.
``StripeEventLog``
    One row per webhook event, for deduplication and replay protection.
``ActiveContactMonth``
    One row per contact per month in which that contact was reached or heard
    from. Workspace-scoped, because a contact belongs to a workspace.

**Nothing here is a ``Plan`` or a ``Price`` table.** Stripe holds the prices and
this app holds the limits (:mod:`apps.billing.plans`); a local mirror of the
price list is a second source of truth that goes wrong the first time somebody
edits an amount in the dashboard, and it goes wrong silently.

**Nothing here is encrypted, which is deliberate and worth reading before
"fixing" it.** The reflex in this codebase is ``EncryptedTextField``, and it is
the wrong reflex for identifiers. ``cus_…`` and ``sub_…`` are opaque handles,
not credentials: holding one grants nothing without ``STRIPE_SECRET_KEY``, which
lives in the environment and never reaches a row. And ``apps.common.encryption``
is explicit that an encrypted field **cannot be filtered** — a fresh nonce per
write means no two ciphertexts of the same value match. The webhook's entire
derivation of "which organization is this event about" is
``filter(stripe_customer_id=...)``, so encrypting that column would force an
``hmac_digest`` sidecar column to look it up again, for no secret kept.
"""

from typing import Any

from django.db import models
from django.utils import timezone

from apps.common.models import BaseModel
from apps.common.scoping import WorkspaceScopedModel

#: Stripe subscription statuses that entitle an organization to the paid plan.
#:
#: ``past_due`` is in here on purpose. A card that bounces puts the subscription
#: into ``past_due`` while Stripe retries it for days, and the Customer Portal is
#: the dunning UI — locking somebody out the hour their renewal failed turns a
#: recoverable payment problem into a support ticket and a cancellation. The
#: statuses that do **not** entitle are ``canceled``, ``unpaid``, ``incomplete``,
#: ``incomplete_expired`` and ``paused``.
#:
#: A status this set does not recognise does not entitle. That is the
#: ``permissions_for_role`` precedent: a value the code does not understand is
#: not a value to guess permissively about.
ENTITLING_STATUSES = frozenset({"active", "trialing", "past_due"})

#: The status of an organization that has never started checkout.
STATUS_NONE = "none"

#: Cap on the stored webhook payload. The whole event is kept for debugging a
#: mis-handled delivery, not as an archive, and an unbounded JSON column on a
#: table one webhook can write to is a disk-exhaustion surface.
MAX_RAW_BYTES = 16_384


class EventStatus(models.TextChoices):
    """What happened to one webhook event after it was verified and recorded."""

    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    IGNORED = "ignored", "Ignored"
    FAILED = "failed", "Failed"


class BillingCustomer(BaseModel):
    """One organization's relationship with Stripe.

    Organization-level, not workspace-level: a plan is bought by an organization
    and covers every workspace under it, which is also why
    ``apps/api/urls_keys.py`` puts API keys at the same tier.

    The row is created lazily, the first time somebody starts checkout — an
    organization that has never opened the billing page has no row, and
    :func:`apps.billing.entitlements.is_paid` reads a missing row as "free"
    rather than creating one to say so.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="billing",
    )
    # Unique and indexed because the webhook path has no session and no
    # organization: it arrives holding a customer id and this column is the only
    # way back to a tenant. See the module docstring on why it is not encrypted.
    stripe_customer_id = models.CharField(max_length=64, unique=True)
    stripe_subscription_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # No `choices`: this is Stripe's vocabulary, not ours, and they add to it.
    # A choices-constrained column would reject a status Stripe invented last
    # week and turn a webhook into a 500. ENTITLING_STATUSES above is the only
    # place the values are interpreted.
    status = models.CharField(max_length=32, default=STATUS_NONE)
    # Display only — tells monthly from yearly on the billing page. Never an
    # entitlement input; `status` is the whole answer to "are they paid".
    price_id = models.CharField(max_length=64, blank=True, default="")
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # Set when a checkout session is created, cleared when the webhook lands.
    # Drives the "being activated" state on the page, and is what
    # `reconcile_pending_checkouts` looks for when a webhook never arrived.
    checkout_pending_since = models.DateTimeField(null=True, blank=True)
    # Stripe's `event.created` for the newest subscription event applied. The
    # out-of-order guard: Stripe guarantees neither delivery nor ordering, and
    # `customer.subscription.updated` routinely lands before the `created` it
    # supersedes.
    last_event_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "billing_customer"

    def __str__(self) -> str:
        return f"{self.stripe_customer_id} ({self.status})"

    @property
    def is_entitled(self) -> bool:
        """Whether this row, on its own, entitles the organization.

        Deliberately does not consult ``current_period_end``. The webhook is the
        authority on whether a subscription is live; a clock-based gate would
        lock a paying customer out during any webhook outage — turning an
        outage on our side into a lockout on theirs.
        """
        return self.status in ENTITLING_STATUSES


class StripeEventLog(BaseModel):
    """One webhook event, recorded before it is acted on.

    The unique constraint on ``event_id`` **is** the deduplication mechanism,
    not hygiene — the same arrangement ``channels.WebhookEventLog`` documents.
    Stripe retries a delivery until it gets a 2xx, so "insert, and skip on
    conflict" is what makes an event take effect exactly once.

    It doubles as replay protection, and its window is the retention period:
    rows older than ``STRIPE_EVENT_LOG_RETENTION_DAYS`` are pruned by
    :func:`apps.billing.housekeeping.prune_stripe_event_log`, so an event
    captured longer ago than that could be replayed. The signature check still
    applies — and Stripe's signature carries a timestamp that
    ``STRIPE_WEBHOOK_TOLERANCE_SECONDS`` bounds to five minutes — so the real
    replay window is the tolerance, and this window only bounds the table.
    """

    event_id = models.CharField(max_length=128, unique=True)
    event_type = models.CharField(max_length=100)
    # Null when the event names a customer this deployment has never seen. That
    # is a normal state, not an error: one Stripe account can serve more than
    # one deployment, and a webhook must never create an organization.
    customer = models.ForeignKey(
        BillingCustomer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=EventStatus.choices, default=EventStatus.RECEIVED)
    raw = models.JSONField(
        default=dict,
        blank=True,
        help_text="Stripe's payload for this event, as delivered. Capped at MAX_RAW_BYTES.",
    )

    class Meta:
        db_table = "billing_stripe_event_log"
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["-received_at"], name="stripeevent_recv_idx")]

    def __str__(self) -> str:
        return f"{self.event_type} {self.event_id} ({self.status})"


class ActiveContactMonth(WorkspaceScopedModel):
    """A contact was reached, or heard from, in a given month.

    **A set, not a counter, and that is the whole design.**
    ``apps/media_library/quotas.py`` argues that a counter is a second source of
    truth which drifts the first time a delete path forgets to decrement it. A
    set has no decrement: membership is asserted with an idempotent
    ``INSERT … ON CONFLICT DO NOTHING``, running it a thousand times for the same
    person changes nothing, and the count is derived rather than stored. Deleting
    a contact cascades its rows away, which is not drift — it is exactly the
    "remove extras" half of the plan's semantics, for free.

    Two cheaper-looking designs that do not work, recorded so they are not
    proposed again:

    * ``Contact.last_interaction_at`` already exists and is already indexed, but
      it is written in exactly one place — ``apps.messaging.ingest``'s
      ``_record_activity``, on the **inbound** path. Counting off it would miss
      every contact a workspace messaged first, which is precisely the broadcast
      and sequence traffic the limit exists to bound.
    * A live ``COUNT(DISTINCT contact_id)`` over messages honours the quotas
      philosophy but has no index behind it and would run before every send. It
      also answers the wrong question: the send path does not ask how many
      contacts are active, it asks whether *this* one already is.

    ``organization`` is denormalised alongside the inherited ``workspace`` FK.
    A plan is bought by an organization, so the limit is an organization-wide
    count — and without this column that count is a loop over the organization's
    workspaces, on a path consulted before every send. It is the one query in
    this app that reads across workspaces, and it does so because the row is a
    billing fact that happens to name a contact.

    ``period`` is the calendar month in the organization's timezone, as
    ``"YYYY-MM"``. Deliberately **not** the Stripe billing period: a free
    organization has no subscription and therefore no period to borrow, and a
    Stripe-derived window would let cancel-and-resubscribe silently reopen a
    month that was already spent.
    """

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="active_contact_months",
    )
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="active_months",
    )
    period = models.CharField(max_length=7)

    class Meta:
        db_table = "billing_active_contact_month"
        constraints = [
            # (contact, period) is the narrowest correct key: a contact belongs
            # to exactly one workspace and therefore one organization, so both
            # of those columns are derivable and are stored only to filter on.
            models.UniqueConstraint(fields=["contact", "period"], name="activecontactmonth_unique_period"),
        ]
        indexes = [models.Index(fields=["organization", "period"], name="activecontact_org_period_idx")]

    def __str__(self) -> str:
        return f"{self.contact_id} in {self.period}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive ``workspace`` and ``organization`` from the contact.

        The production write path is a raw ``INSERT … ON CONFLICT`` in
        :mod:`apps.billing.metering`, which never reaches this method — so this
        exists for tests, the admin and any future caller, and both columns are
        set explicitly there too. Two places, because a derivation that only one
        of them performs is a column that is right in tests and null in
        production.
        """
        if self.contact_id and not self.workspace_id:
            self.workspace_id = self.contact.workspace_id
        if self.workspace_id and not self.organization_id:
            self.organization_id = self.workspace.organization_id
        super().save(*args, **kwargs)
