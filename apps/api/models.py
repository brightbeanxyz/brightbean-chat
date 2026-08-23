"""Models for the public REST API and outbound webhooks (SPEC §5, §17).

Three tables, all workspace-scoped:

``ApiKey``
    The bearer credential. Digest only — the plaintext exists in the response
    that minted it and nowhere else (CONTRIBUTING.md → Secrets).

``OutboundWebhook``
    An operator-configured receiver. Its secret is *recoverable*, unlike an API
    key's: the operator has to be able to put the same value into their own
    verifier, so it is an ``EncryptedTextField`` alongside the digest pattern
    rather than a one-way digest. That is ``ChannelConnection.webhook_secret``'s
    shape, for the same reason.

``WebhookDelivery``
    One row per delivery attempt outcome, capped per webhook by the hourly
    housekeeping sweep. Records status, duration and response code — never the
    request or response body, which can carry a receiver's own credentials in a
    URL or an error string.
"""

from __future__ import annotations

import secrets
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.api.events import SUBSCRIBABLE_EVENTS
from apps.common.encryption import EncryptedTextField, hmac_digest
from apps.common.scoping import WorkspaceScopedModel

__all__ = [
    "ApiKey",
    "ApiScope",
    "DeliveryStatus",
    "OutboundWebhook",
    "WebhookDelivery",
]

#: Longest error string kept on a delivery row. Matches the queue worker's own
#: cap (apps.queueing.worker.MAX_STORED_ERROR_CHARS) — a receiver that returns
#: an HTML error page should not be able to grow this table.
MAX_STORED_ERROR_CHARS = 2000


class ApiScope(models.TextChoices):
    """SPEC §17's coarse scopes.

    Deliberately two, not a mirror of ``PERMISSION_KEYS``. Fine-grained scopes
    are named out of scope by issue #25, and a key that can be granted every
    workspace permission is a key that can eventually mint another key.
    ``apps.api.auth.SCOPE_PERMISSIONS`` is where these become an
    ``effective_permissions`` mapping.
    """

    READ = "read", "Read"
    WRITE = "write", "Write"


class DeliveryStatus(models.TextChoices):
    """Outcome of one delivery attempt.

    ``FAILED`` covers a rejected or unreachable receiver and is retried by the
    queue; ``BLOCKED`` is the SSRF guard refusing the address and is not, since
    retrying a private-range target just repeats the same refusal.
    """

    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked"


def generate_webhook_secret() -> str:
    """A fresh endpoint secret, in the shape the docs tell receivers to expect."""
    return secrets.token_urlsafe(32)


class ApiKey(WorkspaceScopedModel):
    """A bearer credential scoped to one workspace (SPEC §5 ``api_key``).

    **Independent of whoever created it.** ``created_by`` is audit metadata and
    nothing reads it at request time: scopes are capped once, at issuance,
    against the issuer's effective permissions, and after that the key stands
    until it is explicitly revoked. BrightBean Studio re-intersects on every
    request so that demoting the issuer shrinks the key silently; SPEC §5's row
    has no issuer at all, and a self-hosted integration that stops working hours
    after an unrelated staffing change — with nothing in the 403 to explain
    why — is the worse failure. Removing someone's access is a deliberate
    revoke here, not a side effect.

    Revocation is immediate: ``revoked_at`` is read on every request, and there
    is no cache in front of it.
    """

    #: Set only on the instance that just minted the key, never loaded from the
    #: database. The one readable copy the operator will ever see.
    raw_token: str | None = None

    name = models.CharField(max_length=100, help_text='Human label, e.g. "Zapier scenario".')
    scopes = models.JSONField(
        default=list,
        help_text="Subset of ApiScope. See apps.api.auth.SCOPE_PERMISSIONS for what each grants.",
    )
    lookup_prefix = models.CharField(
        max_length=16,
        db_index=True,
        help_text="Non-secret handle derived from the token; the indexed half of verification.",
    )
    token_digest = models.CharField(
        max_length=64,
        unique=True,
        help_text="HMAC of the token's secret part. The stored half; the plaintext is never persisted.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_api_keys",
        help_text="Audit only. The key outlives this user's membership by design; see the class docstring.",
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "api_api_key"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "revoked_at"], name="apikey_ws_revoked_idx")]

    def __str__(self) -> str:
        return f"{self.name} ({self.display_handle})"

    @property
    def display_handle(self) -> str:
        """What the settings page shows in place of a key it cannot recover."""
        from apps.api.keys import TOKEN_PREFIX

        return f"{TOKEN_PREFIX}…{self.lookup_prefix}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def matches(self, secret: str) -> bool:
        """Constant-time comparison of a presented secret against the stored digest.

        Over the digests rather than the raw values, so the comparison is over
        fixed-length data whatever the caller sent.
        """
        from apps.api.keys import digest_for

        if not secret or not self.token_digest:
            return False
        return secrets.compare_digest(digest_for(secret), self.token_digest)


class OutboundWebhook(WorkspaceScopedModel):
    """An operator-configured receiver for catalog events (SPEC §5, §17).

    ``events`` is a list of catalog names, validated against
    :data:`apps.api.events.SUBSCRIBABLE_EVENTS`. It may name an event nothing
    emits yet — ``broadcast.finished`` arrives with L6-B — which is deliberate:
    the subscription is data, so the event starts being delivered the moment its
    emitter lands, with no change here.

    ``url`` is user-supplied, which makes every delivery the exact case
    ``apps.common.outbound.guarded_request`` exists for (SECURITY-BASELINE §6).
    Nothing in this app resolves or connects to it by any other route.
    """

    url = models.URLField(max_length=500, help_text="HTTPS endpoint that receives deliveries.")
    secret = EncryptedTextField(
        help_text="Shared secret the receiver verifies X-BrightBean-Signature with.",
    )
    secret_digest = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="HMAC of secret. Encrypted columns cannot be compared; this is the queryable half.",
    )
    events = models.JSONField(default=list, help_text="Catalog event names this endpoint subscribes to.")
    enabled = models.BooleanField(default=True)
    consecutive_failures = models.PositiveIntegerField(
        default=0,
        help_text="Deliveries that exhausted their retries since the last success. Reset to 0 on any success.",
    )
    disabled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the failure counter switched this endpoint off. Cleared when an operator re-enables it.",
    )
    last_delivery_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "api_outbound_webhook"
        ordering = ["url"]
        indexes = [models.Index(fields=["workspace", "enabled"], name="outboundwebhook_ws_enabled_idx")]

    def __str__(self) -> str:
        return self.url

    def rotate_secret(self) -> str:
        """Mint a new secret, store both halves, and return the plaintext **once**.

        Does not save, matching ``ChannelConnection.rotate_webhook_secret``: the
        caller decides whether this is part of a create or an update, and a
        helper that wrote to the database would be a surprise inside a form's
        ``save(commit=False)``.
        """
        secret = generate_webhook_secret()
        self.secret = secret
        self.secret_digest = hmac_digest(secret)
        return secret

    def subscribes_to(self, event: str) -> bool:
        return self.enabled and event in set(self.events or ())

    @property
    def known_events(self) -> list[str]:
        """Subscribed events that this deployment can actually emit today."""
        return [event for event in (self.events or ()) if event in SUBSCRIBABLE_EVENTS]


class WebhookDelivery(WorkspaceScopedModel):
    """One delivery outcome (SPEC §17's "recent deliveries" log).

    Deliberately records *about* the exchange and never the exchange: no request
    body, no response body. A receiver's error page can echo the URL it was
    called with, and a delivery URL can carry a token in its query string, so a
    stored response body is a credential store nobody asked for.
    """

    webhook = models.ForeignKey(OutboundWebhook, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=DeliveryStatus.choices)
    attempt = models.PositiveSmallIntegerField(default=1)
    response_code = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="Null when the receiver was never reached — DNS, timeout, or the SSRF guard.",
    )
    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="", help_text="Scrubbed and capped; see MAX_STORED_ERROR_CHARS.")

    class Meta:
        db_table = "api_webhook_delivery"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["webhook", "-created_at"], name="webhookdelivery_hook_time_idx")]

    def __str__(self) -> str:
        return f"{self.event} → {self.status} ({self.response_code or '—'})"

    @property
    def succeeded(self) -> bool:
        return self.status == DeliveryStatus.SUCCEEDED


def touch_last_used(api_key: ApiKey, *, now: datetime | None = None) -> None:
    """Stamp ``last_used_at``, debounced to one write per minute per key.

    Without the debounce every authenticated request writes a row, which turns a
    10 req/s rate limit into 10 writes/s of pure bookkeeping on the hot path.
    A minute of staleness on a "last used" column is not a number anyone acts on.
    """
    moment = now or timezone.now()
    previous = api_key.last_used_at
    if previous is not None and (moment - previous).total_seconds() < 60:
        return
    api_key.last_used_at = moment
    # all_objects: this runs on the auth path, where the workspace has just been
    # resolved *from* the key and re-scoping the update would be circular.
    ApiKey.all_objects.filter(pk=api_key.pk).update(last_used_at=moment)
