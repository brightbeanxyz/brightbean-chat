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
from typing import Any

from django.db import models
from django.utils import timezone

from apps.common.encryption import EncryptedJSONField, EncryptedTextField, hmac_digest
from apps.common.models import BaseModel
from apps.common.platforms import Platform
from apps.common.scoping import WorkspaceScopedModel

__all__ = [
    "ChannelConnection",
    "ConnectionStatus",
    "WebhookEventLog",
    "WebhookEventStatus",
    "generate_webhook_secret",
]

#: 32 bytes of urandom, base64url encoded. Comfortably above the 64-bit
#: threshold at which a secret is worth guessing, and short enough to paste into
#: a provider console without wrapping.
WEBHOOK_SECRET_BYTES = 32


def generate_webhook_secret() -> str:
    """A fresh webhook secret. The only place one is minted."""
    return secrets.token_urlsafe(WEBHOOK_SECRET_BYTES)


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

    @property
    def is_active(self) -> bool:
        return self.status == ConnectionStatus.ACTIVE

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

    def mark(self, status: str, **extra: Any) -> None:
        """Record the outcome of processing, touching only what changed."""
        self.status = status
        self.processed_at = timezone.now()
        for name, value in extra.items():
            setattr(self, name, value)
        self.save(update_fields=["status", "processed_at", "updated_at", *extra])
