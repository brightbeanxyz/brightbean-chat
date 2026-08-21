"""Notification storage.

**Nothing here is a** :class:`~apps.common.scoping.WorkspaceScopedModel`.
Notifications are per *user*, not per workspace: the bell reads "everything
addressed to me", across every workspace I belong to. A workspace foreign key
would drag the enforcing manager in with it (ground rule 1), and every bell
render, badge count and mark-read would then need ``.unscoped()`` — turning a
deliberately greppable escape hatch into background noise on the hottest read
path in the app. BrightBean Studio's model carries no workspace key either, and
``docs/SPEC.md`` §5 lists no notification table at all, so there is no schema to
diverge from.

What is lost is a foreign key's integrity and cascade. That is affordable here:
workspaces are archived (``is_archived``), never deleted, so the originating
workspace id can live denormalised in :attr:`Notification.payload` alongside the
action URL and whatever else the event's copy needs.
"""

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class Channel(models.TextChoices):
    """Where a notification is delivered.

    Studio has a third member, ``webhook``, carrying an httpx call and an HMAC
    signer. It is dropped: this product's outbound webhooks are issue #25's
    (SPEC §17), a workspace-level integration rather than a per-user
    notification preference, and porting it would have added the only runtime
    HTTP dependency in the tree.
    """

    IN_APP = "in_app", "In-app"
    EMAIL = "email", "Email"


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    QUEUED = "queued", "Queued"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class Notification(BaseModel):
    """One thing that happened, addressed to one person."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    # No `choices=`, deliberately. The vocabulary lives in
    # apps.notifications.events, which later layers extend by registering; a
    # choices list here would mean a schema migration every time one of them
    # adds an event type, for a constraint Django only enforces in forms.
    event_type = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    # Rendering context plus whatever the row needs to link somewhere:
    # `workspace_id` and `action_url` are the two keys the templates read.
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notifications_notification"
        ordering = ["-created_at"]
        indexes = [
            # The bell's two queries, in order: the recent list, and the unread
            # count that renders on every authenticated page.
            models.Index(fields=["user", "-created_at"], name="notification_user_recent_idx"),
            models.Index(fields=["user", "is_read", "-created_at"], name="notification_user_unread_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.title} -> {self.user.email}"


class NotificationDelivery(BaseModel):
    """The outcome of pushing one notification down one channel.

    A row exists only for channels that leave the process — which today means
    email. In-app "delivery" is the :class:`Notification` row itself: Studio
    creates an ``in_app`` delivery and dispatches it to a function whose whole
    body is ``pass``, which is the tell. Writing that row would cost an insert
    per notification to record something already recorded.

    Retries are **not** tracked here. Issue #5's queue owns backoff (SPEC §15:
    30s, 2m, 10m, 1h, 6h); Studio's own ``next_retry_at`` plus a 1/5/30-minute
    table and a sweep task would be a second, competing schedule. ``attempts``
    is kept as an observation, not as an input to a retry decision.
    """

    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    status = models.CharField(
        max_length=20,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "notifications_delivery"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="notifdelivery_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.notification_id} via {self.channel} ({self.status})"


class NotificationSetting(BaseModel):
    """The single per-user preference issue #7 allows.

    Studio has a preference *matrix* — one row per user per event type per
    channel, plus quiet hours, plus a daily-digest mode — and a settings page to
    drive it. All of it is out of scope here (the issue says so explicitly), and
    a matrix that nothing populates is a table that reads as configured while
    behaving as defaulted.

    Absence means the default, so nothing writes a row until a user actually
    turns email off. Read it through :func:`email_enabled_for` rather than
    ``get_or_create``, which would write on a read path.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_setting",
    )
    email_enabled = models.BooleanField(default=True)

    class Meta:
        db_table = "notifications_setting"

    def __str__(self) -> str:
        state = "on" if self.email_enabled else "off"
        return f"{self.user.email} email={state}"
