"""The messaging spine's tables (SPEC §5, issue #8).

Three rows carry every conversation this product has:

``ContactChannelIdentity``
    Who a contact is *on one channel connection*, plus the two pieces of state
    the compliance engine reads — consent (``opt_in``/``opted_out_at``) and
    recency (``window_expires_at``/``last_inbound_at``). SPEC §5 files this
    table under contacts; it lives here because it hangs off a
    ``ChannelConnection`` and is written only by this app's ingest and facade.
    It still inherits :class:`apps.contacts.models.ContactScopedModel`, which is
    the cross-app abstract base that exists for exactly this shape.
``Conversation``
    One thread: a contact on a connection. Carries the inbox's state
    (``state``, ``assignee``) and the automation kill-switch
    (``automation_paused_until``).
``Message``
    One message in a thread, in either direction, with the idempotency key that
    makes an outbound send exactly-once.

--------------------------------------------------------------------------
Three columns that later layers only read (ROADMAP contract 3)
--------------------------------------------------------------------------

``identity.window_expires_at``, ``identity.opted_out_at`` and
``conversation.automation_paused_until`` are written **only** by
:mod:`apps.messaging.ingest` and :mod:`apps.messaging.services`. The trigger
matcher (L4-A), the flow engine (L3-B) and the inbox (L4-D) read them and never
assign them; ``apps/messaging/tests/test_write_sites.py`` asserts that by
scanning the source tree, because a second write site is invisible in review and
means a messaging window that reopens itself.

--------------------------------------------------------------------------
Why the denormalised columns are derived rather than passed
--------------------------------------------------------------------------

``Message.workspace`` and ``Message.channel_connection`` are both reachable
through ``conversation``, and both are stored anyway: the first because
``WorkspaceScopedModel``'s guard filters ``workspace_id`` and a join cannot
satisfy it, the second because SPEC §5 wants ``provider_message_id`` unique
*scoped per connection* and Django cannot express a constraint across a join.
A denormalisation is a chance for two columns to disagree about whose row this
is, so — exactly as ``ContactScopedModel`` does for ``workspace`` — both are
**derived in save() and never set by a caller**.
"""

from typing import Any, ClassVar

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel
from apps.common.platforms import Platform
from apps.common.scoping import WorkspaceScopedModel
from apps.contacts.models import Contact, ContactScopedModel

__all__ = [
    "DELIVERY_PROGRESS",
    "ContactChannelIdentity",
    "Conversation",
    "ConversationState",
    "Message",
    "MessageDirection",
    "MessageSource",
    "MessageStatus",
    "OptInSource",
    "SendBucket",
]


class OptInSource(models.TextChoices):
    """How consent was obtained (SPEC §5, consent audit per SPEC §11.8).

    Every path that creates or refreshes an identity records one of these
    alongside ``opt_in_at``. "We have consent" is not a fact a support ticket or
    a regulator can act on; "we have consent, captured at 09:14 on the 3rd,
    because they messaged us" is.
    """

    MESSAGE_IN = "message_in", "Inbound message"
    DATA_COLLECTION = "data_collection", "Data collection"
    IMPORT = "import", "Import"
    API = "api", "API"
    MANUAL = "manual", "Manual"


class ConversationState(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"


class MessageDirection(models.TextChoices):
    IN = "in", "Inbound"
    OUT = "out", "Outbound"


class MessageSource(models.TextChoices):
    """What produced an outbound message (SPEC §5).

    An **outbound** vocabulary: an inbound message leaves ``source`` blank,
    because "the contact sent it" is what ``direction`` already says. The
    compliance engine branches on these values, and two of them are load-bearing
    there — ``AGENT`` is the only source Meta's HUMAN_AGENT tag is available to
    (SPEC §22, hard-coded), and ``BROADCAST`` is the only one
    ``PlatformPolicy.broadcast_allowed`` gates.
    """

    AUTOMATION = "automation", "Automation"
    AGENT = "agent", "Agent"
    API = "api", "API"
    BROADCAST = "broadcast", "Broadcast"
    SEQUENCE = "sequence", "Sequence"


class MessageStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    DELIVERED = "delivered", "Delivered"
    READ = "read", "Read"
    FAILED = "failed", "Failed"


#: How far along the delivery ladder each status is. Receipts arrive as
#: ``delivery_status`` webhook events and platforms do not promise to deliver
#: them in order, so :mod:`apps.messaging.ingest` compares ranks and refuses to
#: move a message *backwards* — a "sent" receipt landing after a "read" one must
#: not un-read the message in the inbox. ``FAILED`` is absent on purpose: it is
#: not a rung on this ladder, it is stepping off it, and it is handled
#: explicitly.
DELIVERY_PROGRESS: dict[str, int] = {
    MessageStatus.QUEUED.value: 0,
    MessageStatus.SENT.value: 1,
    MessageStatus.DELIVERED.value: 2,
    MessageStatus.READ.value: 3,
}


class ContactChannelIdentity(ContactScopedModel):
    """One contact, as one channel connection knows them (SPEC §5).

    ``channel_connection`` is **nullable**: ROADMAP contract 1 requires an
    address captured before any connection of that platform exists — a phone
    number typed into a data-collection node on a workspace with no SMS
    connection yet — to be stored as a pending record and upgraded lazily at
    first send. ``ContactScopedModel`` skips its peer-workspace check for a null
    peer; the workspace still comes from the contact.
    """

    peer_field: ClassVar[str] = "channel_connection"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="channel_identities")
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="identities",
    )
    # Denormalised from the connection so a pending record still knows which
    # platform its address belongs to, and so the `window` condition source can
    # filter by platform without a join (it runs inside a correlated subquery).
    platform = models.CharField(max_length=30, choices=Platform.choices)
    #: The platform's own id for this person: a Telegram chat id, a Meta PSID, an
    #: E.164 number, an email address. Attacker-controlled (SECURITY-BASELINE §2)
    #: — stored as delivered, escaped on render, never interpolated anywhere.
    platform_user_id = models.CharField(max_length=200)

    opt_in = models.BooleanField(default=False)
    opt_in_at = models.DateTimeField(null=True, blank=True)
    opt_in_source = models.CharField(max_length=32, choices=OptInSource.choices, blank=True, default="")
    #: Set means blocked on every platform, for every source, with no escape
    #: (SPEC §8). Written only by ingest and the facade — contract 3.
    opted_out_at = models.DateTimeField(null=True, blank=True)
    #: When the platform's messaging window closes. NULL means "no window has
    #: ever been opened", which reads as closed. Written only by
    #: :mod:`apps.messaging.ingest` (SPEC §8: "on every inbound event in the
    #: webhook path. Nowhere else.").
    window_expires_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    #: Platform-supplied profile detail (username, avatar URL). Untrusted, and
    #: the URLs are never fetched server-side (SECURITY-BASELINE §6).
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "messaging_contact_channel_identity"
        constraints = [
            # SPEC §5's unique, restricted to the rows it can apply to. Postgres
            # treats NULLs as distinct in a unique index, so pending rows would
            # slip past an unconditional version of this anyway; saying so
            # explicitly keeps them out of the index and out of the reader's way.
            models.UniqueConstraint(
                fields=["channel_connection", "platform_user_id"],
                condition=models.Q(channel_connection__isnull=False),
                name="identity_unique_conn_user",
            ),
            # The pending half. Keyed on **workspace**, not contact: the point is
            # that one address in one workspace is one person, so two contacts
            # must not each hold a pending identity for the same number. Keying
            # it on contact would permit exactly that, and the upgrade-at-first-
            # send path would then have two rows to choose between. Not
            # deployment-wide, because two workspaces may legitimately both hold
            # a pending SMS identity for the same number.
            models.UniqueConstraint(
                fields=["workspace", "platform", "platform_user_id"],
                condition=models.Q(channel_connection__isnull=True),
                name="identity_unique_pending",
            ),
            # The consent audit (SPEC §11.8) as a database fact rather than a
            # convention. Issue #8's acceptance criteria require every
            # identity-creating path to record when and how consent was given,
            # and #29's GDPR export reads these columns; a path that forgets is a
            # compliance hole that no test of the paths that exist today would
            # catch.
            models.CheckConstraint(
                condition=models.Q(opt_in=False) | (models.Q(opt_in_at__isnull=False) & ~models.Q(opt_in_source="")),
                name="identity_optin_is_audited",
            ),
        ]
        indexes = [
            models.Index(fields=["contact"], name="identity_contact_idx"),
            # The `window` condition source's correlated subquery, verbatim:
            # workspace, platform, window_expires_at.
            models.Index(fields=["workspace", "platform", "window_expires_at"], name="identity_ws_plat_win_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.platform_user_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive ``platform`` from the connection, the way the base derives
        ``workspace`` from the contact.

        A pending row has no connection to read it from, and that is the one
        case where the caller's value stands. Everywhere else an identity whose
        ``platform`` disagreed with its connection would be invisible to the
        ``window`` condition source, which filters on this column.
        """
        connection = self.channel_connection
        if connection is not None:
            self.platform = connection.platform
            update_fields = kwargs.get("update_fields")
            if update_fields:  # never widen an empty set — see the base class
                kwargs["update_fields"] = set(update_fields) | {"platform"}
        super().save(*args, **kwargs)

    @property
    def is_pending(self) -> bool:
        """No connection yet — captured early, upgraded at first send."""
        return self.channel_connection_id is None


class Conversation(ContactScopedModel):
    """One thread: a contact on a connection (SPEC §5).

    ``ContactScopedModel`` rather than a plain ``WorkspaceScopedModel``, because
    a conversation is precisely a contact plus a peer that must belong to the
    same workspace — which is the invariant that base derives and checks.
    """

    peer_field: ClassVar[str] = "channel_connection"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="conversations")
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    state = models.CharField(max_length=16, choices=ConversationState.choices, default=ConversationState.OPEN)
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_conversations",
    )
    #: Set by an agent send (SPEC §14: now + 30 min). The trigger matcher and
    #: execution resumption skip a paused conversation. Written only by
    #: :func:`apps.messaging.services.send_outbound` and
    #: :func:`apps.messaging.services.pause_automation` — contract 3.
    automation_paused_until = models.DateTimeField(null=True, blank=True)
    last_message_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "messaging_conversation"
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "channel_connection"],
                name="conv_unique_contact_conn",
            )
        ]
        indexes = [
            # SPEC §14's inbox list: filter by state, sort by recency.
            models.Index(fields=["workspace", "state", "last_message_at"], name="conv_ws_state_last_idx"),
        ]

    def __str__(self) -> str:
        return f"Conversation {str(self.pk)[:8]}"


class Message(WorkspaceScopedModel):
    """One message in a conversation (SPEC §5, body schema SPEC §7.2).

    ``body`` is exactly what
    :meth:`apps.channels.events.OutboundMessage.to_body` produces, which that
    method's docstring already calls "a persisted contract". Inbound rows use the
    same shape so one renderer serves both directions.

    ``error`` is a **machine-readable code**, not a sentence, matching
    ``SendResult.error``'s convention: a provider's error text routinely quotes
    the request that caused it, credentials included, and this column is rendered
    in the inbox (SECURITY-BASELINE §5).
    """

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    #: Derived from the conversation in save(). Present so SPEC §5's
    #: "unique (provider_message_id) scoped per connection" is expressible.
    channel_connection = models.ForeignKey(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField(max_length=3, choices=MessageDirection.choices)
    #: Blank for inbound — see :class:`MessageSource`.
    source = models.CharField(max_length=16, choices=MessageSource.choices, blank=True, default="")
    body = models.JSONField(default=dict, blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=16, choices=MessageStatus.choices, default=MessageStatus.QUEUED)
    error = models.CharField(max_length=200, blank=True, default="")
    #: Outbound: SPEC §9.4's ``exec:{execution_id}:node:{node_id}:{attempt}``.
    #: Inbound: ``in:{provider_event_id}``, which is what makes a re-dispatched
    #: webhook event produce one row rather than two.
    idempotency_key = models.CharField(max_length=255, blank=True, default="")
    #: An inbox note (SPEC §14): stored as a message, never sent.
    internal = models.BooleanField(default=False)
    #: How many times the provider has been called for this message. The retry
    #: budget lives here rather than on the ``send_retry`` action, because the
    #: *first* send is inline with no action row at all — an action-based budget
    #: would be off by one from birth.
    send_attempts = models.PositiveSmallIntegerField(default=0)
    #: When the provider call was last started. Together with an empty
    #: ``provider_message_id`` this is SPEC §9.4's **unknown outcome**: the call
    #: went out and we never learned what happened to it. Null means the call
    #: definitely has not happened, which is the only state a retry can re-send
    #: from with no duplicate risk at all.
    dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "messaging_message"
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="message_unique_conv_idem",
            ),
            models.UniqueConstraint(
                fields=["channel_connection", "provider_message_id"],
                condition=~models.Q(provider_message_id=""),
                name="message_unique_conn_provider",
            ),
            # ``direction`` and ``source`` are one fact written twice, so the
            # database keeps them from disagreeing. Without it an inbound row
            # could carry ``source="broadcast"`` and show up in analytics as an
            # outbound send.
            models.CheckConstraint(
                condition=models.Q(direction=MessageDirection.IN, source="")
                | (models.Q(direction=MessageDirection.OUT) & ~models.Q(source="")),
                name="message_source_matches_direction",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "created_at"], name="message_conv_created_idx"),
            models.Index(fields=["workspace", "status"], name="message_ws_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.direction} {str(self.pk)[:8]}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Derive the two denormalised columns from the conversation.

        The same discipline ``ContactScopedModel.save()`` applies to
        ``workspace``, for the same reason and with the same ``update_fields``
        caveat: Django reads a *falsy* ``update_fields`` as "save nothing" and
        returns before touching the database, so widening an empty one would
        turn a documented no-op into a real UPDATE.

        Not covered: ``bulk_create``, which bypasses ``save()``. Nothing in this
        app bulk-creates messages; L6-B's broadcast fanout inserts through the
        facade one row at a time because each one needs its own compliance
        decision anyway.
        """
        conversation = self.conversation
        self.workspace_id = conversation.workspace_id
        self.channel_connection_id = conversation.channel_connection_id
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            widened = set(update_fields)
            kwargs["update_fields"] = widened | {"workspace", "channel_connection"} if widened else widened
        super().save(*args, **kwargs)


class SendBucket(BaseModel):
    """A token bucket per channel connection (SPEC §8's rate throttling).

    ``BaseModel``, not ``WorkspaceScopedModel``, and the choice is deliberate
    enough to write down because SECURITY-BASELINE §1 invites the opposite
    reading. This row holds a token count and a timestamp. It is never listed in
    a UI, never filtered by workspace, and always fetched by connection primary
    key — the same shape as ``apps.common.models.RateLimitCounter`` and
    ``apps.channels.models.WebhookEventLog``, neither of which carries a
    workspace column either. Scoping it would put a ``for_workspace()`` on the
    hot path of every send and risk an ``UnscopedQueryError`` inside a worker,
    for no isolation benefit: the connection it hangs off is the tenant
    boundary, and reaching this row means already holding that connection.

    Why a bucket and not ``apps.common.ratelimit``: that module is a **fixed
    window**, which permits a full window's worth of sends in an instant at each
    boundary. A platform that throttles at 25/second does not care that the
    second ticked over. The house pattern it documents — a row, one
    transaction, ``select_for_update`` — is what is borrowed; the arithmetic is
    not.
    """

    connection = models.OneToOneField(
        "channels.ChannelConnection",
        on_delete=models.CASCADE,
        related_name="send_bucket",
    )
    tokens = models.FloatField(default=0.0)
    capacity = models.FloatField()
    #: Tokens per second, from ``PlatformPolicy.rate_default`` unless
    #: ``settings.DEFAULT_SEND_RATE_OVERRIDES`` names the platform.
    refill_rate = models.FloatField()
    #: Explicit rather than ``auto_now``: the refill arithmetic *reads* it, and
    #: a column that updates itself on every save would refill the bucket by
    #: zero seconds every time.
    refilled_at = models.DateTimeField()

    class Meta:
        db_table = "messaging_send_bucket"
        constraints = [
            models.CheckConstraint(condition=models.Q(refill_rate__gt=0), name="bucket_rate_positive"),
            models.CheckConstraint(condition=models.Q(tokens__gte=0), name="bucket_tokens_nonneg"),
        ]

    def __str__(self) -> str:
        return f"{self.refill_rate}/s bucket for connection {self.connection_id}"
