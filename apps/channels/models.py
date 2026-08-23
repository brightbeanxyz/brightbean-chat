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

**WhatsApp's two tables live here too** (issue #19). SPEC §5 files
``whatsapp_template`` under "messaging", but ``apps.messaging`` is the one app
ROADMAP contract 4 keeps free of platform names — the compliance engine's whole
design is that a platform costs a policy row and never a branch — and the table
hangs off a :class:`ChannelConnection` rather than off a conversation. The
*behaviour* is not here: submitting, polling and rendering a template is
:mod:`apps.channels.whatsapp_templates`, so this module stays what it says it
is.
"""

import secrets
from datetime import timedelta
from decimal import Decimal

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
    "FlowPreviewLink",
    "SmsSettings",
    "WebhookEventLog",
    "WebhookEventStatus",
    "WhatsAppCostHint",
    "WhatsAppTemplate",
    "WhatsAppTemplateCategory",
    "WhatsAppTemplateStatus",
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


#: SPEC §6.6's three mandated replies, as the wording a workspace gets before it
#: writes its own. Each has to be intelligible to somebody who is annoyed and
#: has stopped reading, which is why they are short and say the brand rather
#: than the product.
DEFAULT_OPT_OUT_TEXT = "You have been unsubscribed and will get no further messages. Reply START to resubscribe."
DEFAULT_OPT_IN_TEXT = "You are resubscribed and will get messages again. Reply STOP at any time to unsubscribe."
DEFAULT_HELP_TEXT = "Reply STOP to unsubscribe. Message and data rates may apply."


class SmsSettings(WorkspaceScopedModel):
    """One workspace's SMS compliance copy and cost hint (SPEC §6.6, issue #20).

    Per workspace rather than per connection, and that is a compliance judgement
    rather than a modelling shortcut: a contact who texts STOP to one of a
    workspace's numbers is unsubscribing from *that number*, but the sentence
    they get back is the workspace's voice, and two numbers answering HELP with
    different descriptions of the same business is exactly what a carrier audit
    picks up on.

    Everything here is **copy and hints**. Nothing on this row can weaken the
    compliance behaviour: the keywords are hard-coded in
    :mod:`apps.channels.providers.sms`, the suppression happens in
    ``apps.messaging.ingest`` whatever this says, and a workspace that blanks
    every field gets the defaults below rather than silence. SPEC §19 puts
    opt-out enforcement at a chokepoint precisely so it is not configurable.

    ``per_segment_cost`` is a **hint**, and the same decision SPEC §6.5 makes for
    WhatsApp: "OpenChat only warns, never meters". A self-hoster's Twilio bill is
    theirs, prices differ per destination and per campaign, and a number in a
    composer that pretended to be authoritative would be wrong more often than
    it was right. It is stored without a currency for the same reason the
    workspace already stores none — the deployment knows, the product does not.
    """

    help_text_body = models.TextField(
        blank=True,
        default="",
        help_text="Sent when a contact texts HELP. Blank uses the default wording.",
    )
    opt_out_confirmation = models.TextField(
        blank=True,
        default="",
        help_text="Sent once when a contact texts STOP. Blank uses the default wording.",
    )
    opt_in_confirmation = models.TextField(
        blank=True,
        default="",
        help_text="Sent when a contact texts START to resubscribe. Blank uses the default wording.",
    )
    per_segment_cost = models.DecimalField(
        max_digits=8,
        decimal_places=5,
        null=True,
        blank=True,
        help_text="What one segment costs on this account, for the composer's estimate. A hint, never a meter.",
    )
    #: SPEC §6.6: "OpenChat surfaces a settings checklist only." US A2P 10DLC
    #: registration happens in the Twilio console and cannot be done from here,
    #: so these are an operator's own record that they did it — read by nothing
    #: and enforced by nothing, which the settings page says out loud.
    a2p_brand_registered = models.BooleanField(default=False)
    a2p_campaign_approved = models.BooleanField(default=False)

    class Meta:
        db_table = "channels_sms_settings"
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="smssettings_unique_workspace"),
        ]

    def __str__(self) -> str:
        return f"SMS settings for {self.workspace_id}"

    @property
    def help_reply(self) -> str:
        return self.help_text_body.strip() or DEFAULT_HELP_TEXT

    @property
    def opt_out_reply(self) -> str:
        return self.opt_out_confirmation.strip() or DEFAULT_OPT_OUT_TEXT

    @property
    def opt_in_reply(self) -> str:
        return self.opt_in_confirmation.strip() or DEFAULT_OPT_IN_TEXT


# ---------------------------------------------------------------------------
# WhatsApp templates (SPEC §5's whatsapp_template, §6.5) — issue #19
# ---------------------------------------------------------------------------


class WhatsAppTemplateCategory(models.TextChoices):
    """The three categories Meta will review a template under (SPEC §6.5).

    Category is not decoration: it decides the price of every send and it
    decides how the template is reviewed. Marketing is the expensive,
    strictly-reviewed one; authentication is one-time-passcode traffic and has
    its own rules Meta enforces at submission.
    """

    MARKETING = "marketing", "Marketing"
    UTILITY = "utility", "Utility"
    AUTHENTICATION = "authentication", "Authentication"


class WhatsAppTemplateStatus(models.TextChoices):
    """Where a template is in its life (SPEC §5).

    ``DRAFT`` is ours alone — Meta has never seen it. The other three mirror
    what the Graph API reports back, and the transition between them is made by
    the hourly poll rather than by anything a person does here
    (:mod:`apps.channels.whatsapp_templates`).
    """

    DRAFT = "draft", "Draft"
    PENDING = "pending", "In review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class WhatsAppTemplate(WorkspaceScopedModel):
    """One WhatsApp message template, ours and Meta's copy of it (SPEC §6.5).

    Outside the 24-hour window a WhatsApp send needs one of these and nothing
    else will do — ``apps.messaging.compliance`` answers ``NeedsTemplate`` from
    ``PlatformPolicy`` alone, with no knowledge that this table exists. That
    separation is the point: the *decision* is policy data, and this is the
    material that satisfies it.

    **Workspace-scoped, though SPEC §5 lists only the connection FK.** Every
    read here happens in a settings page or a composer that already knows its
    workspace, and CONTRIBUTING makes the enforcing manager the rule for tenant
    data rather than something each view remembers. The connection FK is still
    the authoritative link — a template belongs to the WABA it was submitted to.

    **What keeps the two from disagreeing is the write path, not a ``clean``.**
    An earlier version of this docstring promised one, and a ``clean`` here
    could not have delivered it anyway: ``workspace`` is not a form field and is
    assigned after ``form.is_valid()``, so ``ModelForm._post_clean`` would run
    the model's validation before the value it was meant to check exists. What
    holds instead is that both sides are forced to the same workspace on every
    write — ``views_whatsapp._edit`` loads through ``get_scoped_object_or_404``,
    ``WhatsAppTemplateForm`` narrows the ``channel_connection`` choices to that
    workspace, and the view then assigns ``workspace`` itself. Every reader
    fails closed regardless: ``whatsapp_templates.sendable`` and
    ``approved_templates_for`` both filter on workspace *and* connection, so a
    row that ever did disagree would be unsendable and invisible rather than
    reachable from the wrong tenant.

    ``body_structure`` is the authored template, in the shape
    :mod:`apps.channels.whatsapp_templates` translates into Graph components::

        {
          "header": {"format": "text", "text": "Order {{1}}"},   # optional
          "body":   {"text": "Hi {{1}}, your order shipped."},
          "footer": {"text": "Reply STOP to opt out."},           # optional
          "buttons": [{"type": "quick_reply", "text": "Track"},
                      {"type": "url", "text": "Open", "url": "https://x.test/{{1}}"}]
        }

    The ``{{n}}`` placeholders are Meta's own numbering, and they are filled by
    the one shared renderer (SECURITY-BASELINE §3) — never by a template engine.
    """

    channel_connection = models.ForeignKey(
        ChannelConnection,
        on_delete=models.CASCADE,
        related_name="whatsapp_templates",
        help_text="The WhatsApp connection whose WABA this template was submitted to.",
    )
    name = models.CharField(
        max_length=512,
        help_text="Meta's template name: lowercase letters, digits and underscores.",
    )
    language = models.CharField(
        max_length=10,
        default="en_US",
        help_text="Meta's language code, e.g. en_US or de.",
    )
    category = models.CharField(max_length=20, choices=WhatsAppTemplateCategory.choices)
    body_structure = models.JSONField(
        default=dict,
        blank=True,
        help_text="Header/body/footer/buttons with {{n}} placeholders. See the class docstring.",
    )
    meta_template_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Meta's id for this template. Empty until it has been submitted.",
    )
    status = models.CharField(
        max_length=20,
        choices=WhatsAppTemplateStatus.choices,
        default=WhatsAppTemplateStatus.DRAFT,
    )
    rejected_reason = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text="Meta's stated reason, shown to the operator. Provider-supplied: escape on render.",
    )

    class Meta:
        db_table = "channels_whatsapp_template"
        ordering = ["name", "language"]
        constraints = [
            # Meta's own key for a template is (name, language) inside one
            # WABA, so two rows agreeing on all three would be one template
            # with two local states — and the poll would flip it back and
            # forth. Per connection rather than per workspace: two numbers on
            # different WABAs legitimately have a template of the same name.
            models.UniqueConstraint(
                fields=["channel_connection", "name", "language"],
                name="whatsapptemplate_unique_name_language",
            ),
        ]
        indexes = [
            # The hourly poll reads exactly this shape.
            models.Index(fields=["status"], name="whatsapptemplate_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.language})"

    @property
    def reference(self) -> str:
        """``<name>/<language>`` — what ``OutboundMessage.template_ref`` carries.

        Deliberately not the primary key. A queued message is retried minutes or
        hours later from its stored body alone, and a template deleted in the
        meantime would leave that retry unable to say what it was sending. Name
        and language are also what the Cloud API itself keys on, so the value
        that survives is the value the platform understands.
        """
        return f"{self.name}/{self.language}"

    @property
    def is_usable(self) -> bool:
        """True when a send may reference this template."""
        return self.status == WhatsAppTemplateStatus.APPROVED


class WhatsAppCostHint(WorkspaceScopedModel):
    """A workspace's own per-category price estimates (SPEC §6.5, §22).

        Surface per-send cost hint in broadcast composer (static table per
        category, editable in settings; do not attempt live pricing).

    SPEC §22 settles what this is for: "WhatsApp costs are the self-hoster's
    Meta bill; OpenChat only warns, never meters." So these numbers are shown
    beside a template and multiplied by a recipient count in a composer, and
    nothing in the product ever adds them up, stores them per message, or
    refuses a send because of them.

    They are per workspace and hand-entered because Meta prices per country,
    per category and per agreement, and revises all three. A number this
    product fetched would be wrong in a way that looked authoritative; a number
    the operator typed from their own rate card is wrong in a way they can see.

    Explicit columns rather than a JSON blob of categories: config authored by
    a user gets schema validation that rejects unknown keys
    (SECURITY-BASELINE §7), and three decimals do not need a document to hold
    them.
    """

    #: What a category costs when the workspace has entered nothing. Zero, not a
    #: guess: a made-up price shown as a hint is worse than an absent one, and
    #: the settings page says so where an operator will read it.
    DEFAULT_AMOUNT = Decimal("0")

    currency = models.CharField(
        max_length=3,
        default="USD",
        help_text="ISO 4217 code, for display only. Nothing converts between currencies.",
    )
    marketing = models.DecimalField(max_digits=8, decimal_places=4, default=DEFAULT_AMOUNT)
    utility = models.DecimalField(max_digits=8, decimal_places=4, default=DEFAULT_AMOUNT)
    authentication = models.DecimalField(max_digits=8, decimal_places=4, default=DEFAULT_AMOUNT)

    class Meta:
        db_table = "channels_whatsapp_cost_hint"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["workspace"], name="whatsappcosthint_unique_workspace"),
        ]

    def __str__(self) -> str:
        return f"WhatsApp cost hints ({self.currency})"

    def amount_for(self, category: str) -> Decimal:
        """The per-send estimate for one category, or the default.

        Looked up against the choices rather than by bare ``getattr``, for the
        reason ``Capabilities.max_bytes_for`` gives: the field names are the
        category values, and an unconstrained lookup would happily return
        ``currency``.
        """
        if category not in WhatsAppTemplateCategory.values:
            return self.DEFAULT_AMOUNT
        value = getattr(self, category, None)
        return value if isinstance(value, Decimal) else self.DEFAULT_AMOUNT
