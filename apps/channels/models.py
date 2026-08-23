"""Channel connections and the raw webhook event log (SPEC §5).

Two tables with deliberately different tenancy:

:class:`ChannelConnection` is tenant data — a workspace's bot, page or number,
holding the credentials that are this deployment's crown jewels — so it inherits
:class:`~apps.common.scoping.WorkspaceScopedModel` and its querysets refuse to
run unscoped.

:class:`WebhookEventLog` hangs off the connection and carries no workspace
column, which is what SPEC §5 specifies (it is listed under "webhooks and API",
not with the tenant tables). That is also what keeps the scoping guard off the
webhook hot path: the request that writes these rows has no authenticated user
and therefore no workspace to scope by, and reaching for ``.unscoped()`` on
every inbound event would make the greppable escape hatch meaningless.

**The webhook secret is stored twice, on purpose.** ``webhook_secret`` is
encrypted at rest and is what an operator pastes into a provider console.
``webhook_secret_digest`` is the queryable HMAC of the same value, because
encrypted columns cannot be filtered — every write uses a fresh nonce, so
``.filter(webhook_secret=value)`` compares two unrelated ciphertexts and
silently matches nothing (see ``apps.common.encryption``). Telegram presents its
secret in a header and the connection has to be found *by* it, which is exactly
the case ``apps/credentials/models.py`` predicted this issue would be the first
to hit.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedJSONField, EncryptedTextField, hmac_digest
from apps.common.models import BaseModel
from apps.common.platforms import Platform
from apps.common.scoping import WorkspaceScopedModel

__all__ = [
    "ChannelConnection",
    "ConnectionStatus",
    "EmailSuppression",
    "SuppressionReason",
    "FlowPreviewLink",
    "WebhookEventLog",
    "WebhookEventStatus",
    "generate_webhook_secret",
    "generate_preview_handle",
]

#: 32 bytes of urandom, base64url encoded. Comfortably above the 64-bit
#: threshold at which a secret is worth guessing, and short enough to paste into
#: a provider console without wrapping.
WEBHOOK_SECRET_BYTES = 32

#: 24 bytes of urandom, base64url encoded: 32 characters, all of them inside
#: Telegram's ``[A-Za-z0-9_-]`` deep-link alphabet and comfortably inside its
#: 64-character ``start`` payload budget. See :class:`FlowPreviewLink`.
PREVIEW_HANDLE_BYTES = 24

#: How long a "test on Telegram" link stays usable. Short on purpose: it is
#: clicked within seconds of being generated, and it is minted again by pressing
#: the button again.
PREVIEW_LINK_TTL = timedelta(minutes=15)


def generate_webhook_secret() -> str:
    """A fresh webhook secret. The only place one is minted."""
    return secrets.token_urlsafe(WEBHOOK_SECRET_BYTES)


def generate_preview_handle() -> str:
    """A fresh preview-link handle. The only place one is minted."""
    return secrets.token_urlsafe(PREVIEW_HANDLE_BYTES)


class ConnectionStatus(models.TextChoices):
    """SPEC §5's ``channel_connection.status``.

    ``NEEDS_REAUTH`` is set by an adapter when the platform rejects the stored
    credentials — it is the state the notification in #7 announces and the
    reconnect button in each adapter's UI clears. Nothing in this issue sets it;
    the value exists so Layer 4 does not have to migrate for it.
    """

    ACTIVE = "active", "Active"
    NEEDS_REAUTH = "needs_reauth", "Needs reconnection"
    DISABLED = "disabled", "Disabled"


class WebhookEventStatus(models.TextChoices):
    """SPEC §5's ``webhook_event_log.status``."""

    RECEIVED = "received", "Received"
    PROCESSED = "processed", "Processed"
    SKIPPED_DUPLICATE = "skipped_duplicate", "Skipped (duplicate)"
    FAILED = "failed", "Failed"


class ChannelConnection(WorkspaceScopedModel):
    """One connected bot, page, number or sending domain (SPEC §5)."""

    platform = models.CharField(max_length=30, choices=Platform.choices)
    display_name = models.CharField(max_length=200)
    external_id = models.CharField(
        max_length=200,
        help_text="Page id, IG user id, WABA phone number id, bot id, Twilio number or sending domain.",
    )
    credentials = EncryptedJSONField(
        default=dict,
        blank=True,
        help_text="Encrypted JSON of the platform's per-connection credentials.",
    )
    status = models.CharField(max_length=20, choices=ConnectionStatus.choices, default=ConnectionStatus.ACTIVE)
    capabilities_cache = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "What the platform reported this specific connection can do. The static table in "
            "apps.channels.capabilities is the default; this narrows it per connection where a "
            "platform says so (an unverified WhatsApp number, a page without messaging permissions)."
        ),
    )
    webhook_secret = EncryptedTextField(blank=True, default="")
    webhook_secret_digest = models.CharField(
        max_length=64,
        blank=True,
        default="",
        # No db_index: the partial unique constraint below already indexes this
        # column, and every lookup filters on a non-empty digest, which implies
        # the constraint's predicate. A second index would be dead weight on
        # the write path of a table the webhook endpoint writes to.
        help_text="HMAC of webhook_secret. The queryable half; see the module docstring.",
    )

    class Meta:
        db_table = "channels_channel_connection"
        ordering = ["platform", "display_name"]
        constraints = [
            # SPEC §5: unique (platform, external_id) — deployment-wide, not
            # per workspace. One Telegram bot cannot serve two workspaces, and
            # the second workspace connecting it would silently steal the first
            # one's inbound traffic. The cost is that a failed create tells the
            # operator the id is taken somewhere in this deployment; the form
            # error is worded so it never says where (SECURITY-BASELINE §1).
            models.UniqueConstraint(fields=["platform", "external_id"], name="channelconnection_unique_platform_ext"),
            models.UniqueConstraint(
                fields=["webhook_secret_digest"],
                condition=models.Q(webhook_secret_digest__gt=""),
                name="channelconnection_unique_webhook_digest",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "platform"], name="channelconn_ws_platform_idx")]

    def __str__(self) -> str:
        return f"{self.get_platform_display()} · {self.display_name}"

    def rotate_webhook_secret(self) -> str:
        """Mint a new secret, store both halves, and return the plaintext **once**.

        The caller gets the only readable copy it will ever see. Nothing
        re-renders it afterwards (CONTRIBUTING: "Never render a stored secret"),
        so an operator who loses it rotates rather than looks it up.

        Does not save: the caller decides whether this is part of a create or an
        update, and a helper that wrote to the database would be a surprise
        inside a form's ``save(commit=False)``.
        """
        secret = generate_webhook_secret()
        self.webhook_secret = secret
        self.webhook_secret_digest = hmac_digest(secret)
        return secret

    @classmethod
    def resolve_by_webhook_secret(cls, secret: str) -> "ChannelConnection | None":
        """Find the connection a presented webhook secret belongs to.

        The inbound webhook path has no session and therefore no workspace, so
        this genuinely crosses tenants — ``.unscoped()``, deliberately and
        greppably (CONTRIBUTING). What bounds it is the secret itself: without a
        matching digest there is no row, and the digest is keyed on
        ``SECRET_KEY``, so a database dump does not let anyone recompute one.

        An empty or absent secret returns None rather than matching the rows
        whose digest is also empty.
        """
        if not secret:
            return None
        return (
            # Cross-tenant by necessity: an inbound webhook identifies itself
            # with a secret, not a session.
            cls.objects.unscoped().filter(webhook_secret_digest=hmac_digest(secret)).first()
        )

    def verify_webhook_secret(self, presented: str) -> bool:
        """Constant-time comparison against the stored secret.

        Used by adapters whose platform sends the secret back verbatim in a
        header (Telegram). Goes through the digest rather than the decrypted
        value so the comparison is over fixed-length data.
        """
        if not presented or not self.webhook_secret_digest:
            return False
        return secrets.compare_digest(hmac_digest(presented), self.webhook_secret_digest)


class WebhookEventLog(BaseModel):
    """One inbound webhook event, raw, before anything interpreted it (SPEC §5).

    The unique constraint is the deduplication mechanism, not a hygiene measure:
    every platform retries deliveries, and SPEC §7.1 step 2 makes "insert, and
    skip on conflict" the thing that guarantees an event is processed once.

    **It doubles as replay protection, and its window is the retention period.**
    Rows older than ``WEBHOOK_EVENT_LOG_RETENTION_DAYS`` (30 by default) are
    pruned by :func:`apps.channels.housekeeping.prune_webhook_event_log`, so an
    attacker replaying a delivery captured more than 30 days ago would get it
    processed again. Signature verification still applies — the replay has to
    carry a valid signature over the same body — which is why the window is a
    documented tradeoff rather than a hole: the alternative is a table that
    grows without bound for a threat the signature already bounds.
    """

    connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="webhook_events",
    )
    platform = models.CharField(max_length=30, choices=Platform.choices)
    provider_event_id = models.CharField(max_length=200)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=WebhookEventStatus.choices, default=WebhookEventStatus.RECEIVED)
    raw = models.JSONField(
        default=dict,
        blank=True,
        help_text="The provider's payload for this event, as delivered. Attacker-controlled: escape on render.",
    )

    class Meta:
        db_table = "channels_webhook_event_log"
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["connection", "provider_event_id"],
                name="webhookeventlog_unique_connection_event",
            ),
        ]
        indexes = [models.Index(fields=["connection", "-received_at"], name="webhookevent_conn_recv_idx")]

    def __str__(self) -> str:
        return f"{self.platform}:{self.provider_event_id} ({self.status})"


class SuppressionReason(models.TextChoices):
    """Why an address stopped being mailable.

    Kept apart from ``OptInSource`` on the identity: that column records how
    consent was *obtained*, and overwriting it at the moment consent stopped
    applying would destroy the pair a regulator asks to see together
    (``apps.messaging.services.record_opt_out`` makes the same argument).
    """

    HARD_BOUNCE = "hard_bounce", "Hard bounce"
    SOFT_BOUNCE = "soft_bounce", "Repeated soft bounce"
    COMPLAINT = "complaint", "Spam complaint"
    UNSUBSCRIBE = "unsubscribe", "Unsubscribed"
    MANUAL = "manual", "Added by hand"


class EmailSuppression(WorkspaceScopedModel):
    """One mailbox this workspace must not write to again (SPEC §6.7).

    --------------------------------------------------------------------------
    Why this is not a column on the identity
    --------------------------------------------------------------------------

    ``ContactChannelIdentity.opted_out_at`` already answers "did this identity
    withdraw consent?", and for an unsubscribe it is set too. It cannot be the
    whole answer, because of a decision made two apps away:
    ``apps/contacts/imports.py`` **never fabricates an identity** — "a spreadsheet
    column is not consent" — and ``imports._match`` deliberately skips deleted
    contacts. So a contact that goes away and comes back is a brand-new
    ``Contact`` row, and the opt-out is out of its reach two different ways:

    * ``delete_contact`` is a *soft* delete, so the old identity survives — but
      it belongs to a tombstone, and identities resolve **by contact**, so the
      re-imported contact has none and cannot be given one (the
      ``(connection, address)`` unique constraint is already taken).
    * A **hard** delete — issue #29's GDPR erasure, or a merge — takes the
      identity with it, and every trace of the opt-out goes too.

    A bounce is not a fact about a contact row. It is a fact about a **mailbox**:
    that address rejected mail, or its owner marked us as spam, and neither
    stops being true because somebody re-uploaded a spreadsheet. So the key is
    the normalised address and there is deliberately no foreign key to
    ``Contact`` — a cascade from one would delete exactly the record that has to
    survive.

    Workspace-scoped rather than deployment-wide, for the reason
    ``messaging.models``' pending-identity constraint gives for the same choice:
    two workspaces may legitimately both hold the same address, they send from
    different domains, and one tenant's bounce is not evidence about another's
    relationship with that mailbox.

    **Enforcement** is in ``apps.channels.providers.email``, immediately before
    the wire, and a hit there also opts the identity out through the messaging
    facade — so the *second* send to a re-imported suppressed address is refused
    by the compliance engine (SPEC §19's chokepoint) rather than by the adapter,
    and every set-wise consumer sees it too.
    """

    #: The mailbox, through ``apps.common.addresses.normalize_email``. Plain text
    #: rather than encrypted for the reason CONTRIBUTING gives: this column is
    #: looked up by value on every send, and an encrypted column cannot be
    #: filtered. It is the same exposure ``contact.email`` already carries.
    address = models.CharField(max_length=320)
    reason = models.CharField(max_length=20, choices=SuppressionReason.choices)
    #: The provider's own code or bounce subtype, for an operator working out
    #: why. A code, never the provider's prose: a diagnostic string routinely
    #: quotes the message that bounced (SECURITY-BASELINE §5).
    detail = models.CharField(max_length=200, blank=True, default="")
    #: The connection that was sending when this was recorded, for support. Kept
    #: nullable and ``SET_NULL``: disconnecting a channel must not delete the
    #: suppression list it produced.
    connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="email_suppressions",
    )

    class Meta:
        db_table = "channels_email_suppression"
        ordering = ["address"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "address"],
                name="emailsuppression_unique_workspace_address",
            ),
        ]
        indexes = [models.Index(fields=["workspace", "address"], name="emailsuppress_ws_addr_idx")]

    def __str__(self) -> str:
        return f"{self.address} ({self.reason})"


class FlowPreviewLink(WorkspaceScopedModel):
    """One "test on Telegram" link: a tester's chat bound to a draft flow (SPEC §16).

    SPEC §16 asks for "a test on Telegram action that links the editor's user to
    a test conversation and runs the draft version against it". This row is that
    link, and it exists because the binding has to survive the round trip out
    through Telegram and back: the builder mints it, the tester taps a ``t.me``
    link, and the ``/start`` that arrives seconds later is the first time the
    server learns which chat belongs to which editor.

    **Why a handle and not a signed token.** SECURITY-BASELINE §4 puts every
    unauthenticated token route on ``apps.common.signing``, and issue #12's own
    wording is ``?start=preview-<signed token>``. Telegram makes that impossible:
    a deep-link ``start`` payload is capped at **64 characters** and restricted
    to ``[A-Za-z0-9_-]``, while a ``django.core.signing`` token is longer than
    that and contains ``:`` and ``.``. So the link carries a 32-character random
    handle instead, and this row carries everything the token would have.

    Every property the baseline is protecting survives the substitution:

    expiry
        ``expires_at``, checked on use. Shorter than a signed token's typical
        life, not longer.
    constant-time verification
        The handle is looked up by ``hmac_digest``, an equality match on a
        fixed-length column — the same construction ``ChannelConnection``
        already uses for its webhook secret, and for the same reason: an
        encrypted column cannot be filtered.
    generic failure
        Nothing about a bad handle is distinguishable from a good one that has
        expired, or from an unrecognised ``/start`` payload. A failed preview
        does not answer, log a distinct reason, or behave differently in any way
        an outsider can see (:mod:`apps.channels.preview`).
    unguessable
        192 bits of ``secrets.token_urlsafe``, and the digest is keyed on
        ``SECRET_KEY``, so a database dump gives up neither the handles nor the
        ability to recompute one.

    **Isolation per tester** is ``chat_id``. It is empty until the first
    ``/start`` claims the link, and the claim is a conditional UPDATE rather
    than a read followed by a write, so two chats racing the same handle cannot
    both win. Afterwards only that chat can use the link again, which is what
    keeps one editor's preview out of another person's conversation.
    """

    flow = models.ForeignKey("flows.Flow", on_delete=models.CASCADE, related_name="preview_links")
    channel_connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="preview_links",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="flow_preview_links",
        help_text="The editor who pressed Test. SPEC §16's 'links the editor's user'.",
    )
    handle_digest = models.CharField(
        max_length=64,
        unique=True,
        help_text="HMAC of the deep-link handle. The queryable half; see the class docstring.",
    )
    # No db_index: the named index below already covers this column, and the
    # only query that reads it filters on nothing else. A second index would be
    # dead weight on the write path — the same call ChannelConnection makes for
    # webhook_secret_digest.
    expires_at = models.DateTimeField()
    chat_id = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="The Telegram chat that claimed this link. Empty until the first /start.",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "channels_flow_preview_link"
        ordering = ["-created_at"]
        indexes = [
            # The housekeeping sweep reads exactly this shape.
            models.Index(fields=["expires_at"], name="previewlink_expires_idx"),
        ]

    def __str__(self) -> str:
        return f"preview of {self.flow_id} ({'claimed' if self.chat_id else 'unclaimed'})"
