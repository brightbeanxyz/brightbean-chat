"""Everything the CRM reads out of :mod:`apps.messaging` and :mod:`apps.flows`.

One module, so the coupling is greppable. The contact detail page wants a
person's channel identities, their recent messages and whatever automation is
running for them; the contact list wants a channel-icon column and an opt-in
column. All of that lives in two other apps, and scattering the imports across
``views.py`` would leave this app quietly depending on both with nothing saying
so in one place.

--------------------------------------------------------------------------
Read-only, and how that is kept true
--------------------------------------------------------------------------

Nothing here assigns a messaging or flow column. ROADMAP contract 3 pins
``identity.window_expires_at``, ``identity.opted_out_at`` and
``conversation.automation_paused_until`` to single write sites, and
``apps/messaging/tests/test_write_sites.py`` scans the source tree to prove it.
So the two mutations this page offers go through the owning app's public door:

* opting an identity out is :func:`apps.messaging.services.record_opt_out`,
  which delegates to the one write site in ``messaging.ingest``;
* stopping automation is :func:`apps.flows.engine.stop_automation`, which expires
  the run **and cancels the queue rows that would have resumed it**. A view
  setting ``execution.status`` itself would leave those rows armed, so the run
  the operator believed they had stopped would wake on its next timer.

--------------------------------------------------------------------------
``installed_model``, not ``try: import``
--------------------------------------------------------------------------

``apps.flows.compat.installed_model`` is the house pattern for reaching an app
that may not be installed: it answers "not yet" rather than raising, and starts
answering for real the moment the app appears, with no edit at the call site.
Both apps are installed in this deployment; the guard is what keeps
``apps.contacts`` importable in one where they are not, which is the property
``services.py`` already maintains for the merge path.

--------------------------------------------------------------------------
``Exists()`` and the scoping guard
--------------------------------------------------------------------------

Every subquery below is built with ``.for_workspace(...)``. This is not
belt-and-braces: ``WorkspaceScopedQuerySet`` refuses to *execute* unscoped, and a
queryset handed to ``Exists()`` is never executed — it is compiled into the outer
statement — so the guard does not fire and that predicate is the subquery's only
tenancy check. :mod:`apps.contacts.conditions` explains at length.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.contacts.models import Contact
from apps.flows.compat import installed_model

logger = logging.getLogger(__name__)

__all__ = [
    "ContactChannel",
    "MessagePreview",
    "annotate_reachability",
    "avatar_url",
    "identities_for",
    "identity_for",
    "live_execution",
    "opt_out",
    "platforms_for",
    "recent_messages",
    "start_flow_for",
    "startable_flow",
    "startable_flows",
    "stop_automation",
]

#: Rows shown in the activity pane. A window, not a thread — the thread is the
#: inbox's (issue #14), and this page links into it rather than reproducing it.
RECENT_MESSAGE_LIMIT = 20

#: Schemes an identity's ``extra.profile_pic_url`` may use before it becomes a
#: ``src``. Platform-supplied and therefore attacker-controlled
#: (SECURITY-BASELINE §2): ``javascript:`` and ``data:`` in an ``img`` are the
#: two that turn a profile picture into script execution and a phishing canvas.
SAFE_URL_SCHEMES = frozenset({"http", "https"})


def _identity_model() -> Any:
    return installed_model("messaging", "apps.messaging", "ContactChannelIdentity")


def _message_model() -> Any:
    return installed_model("messaging", "apps.messaging", "Message")


def _execution_model() -> Any:
    return installed_model("flows", "apps.flows", "FlowExecution")


# ---------------------------------------------------------------------------
# Presentation shapes. Views and templates read these, never model internals.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContactChannel:
    """One identity, as the detail page shows it.

    A dataclass rather than the model, because "is the window open and for how
    much longer" is a question about *now* that the row cannot answer on its
    own, and computing it in the template would mean a comparison against
    ``timezone.now()`` in four places that could each be slightly different.
    """

    identity: Any
    platform: str
    address: str
    username: str
    opt_in: bool
    opted_out_at: Any
    window_expires_at: Any
    window_open: bool
    is_pending: bool

    @property
    def is_opted_out(self) -> bool:
        return self.opted_out_at is not None

    @property
    def reachable(self) -> bool:
        """Consent given, consent not withdrawn. Not a send decision.

        The send decision is :func:`apps.messaging.compliance.can_send`, which
        also weighs the window, the platform's policy and the message's source.
        This is only what the CRM column means: "this person said we may".
        """
        return self.opt_in and self.opted_out_at is None


@dataclass(frozen=True)
class MessagePreview:
    """One row in the activity pane."""

    message: Any
    conversation: Any
    inbound: bool
    text: str
    attachment_count: int
    created_at: Any


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


def identities_for(contact: Contact) -> list[ContactChannel]:
    """Every channel identity this contact has, newest connection first."""
    model = _identity_model()
    if model is None:
        return []
    now = timezone.now()
    rows = (
        model.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact)
        .select_related("channel_connection")
        .order_by("platform", "created_at")
    )
    return [_as_channel(row, now) for row in rows]


def _as_channel(identity: Any, now: Any) -> ContactChannel:
    extra = identity.extra if isinstance(identity.extra, dict) else {}
    username = extra.get("username") or ""
    expires = identity.window_expires_at
    return ContactChannel(
        identity=identity,
        platform=identity.platform,
        address=identity.platform_user_id,
        # str() rather than trusting the column: `extra` is platform-supplied
        # jsonb, so "username" can legitimately arrive as a number or a list and
        # a template rendering a list would show its repr.
        username=str(username)[:200],
        opt_in=identity.opt_in,
        opted_out_at=identity.opted_out_at,
        window_expires_at=expires,
        # NULL means "no window has ever been opened", which reads as closed —
        # the same reading apps.messaging.compliance uses.
        window_open=expires is not None and expires > now,
        is_pending=identity.is_pending,
    )


def platforms_for(contacts: Iterable[Contact], workspace: Any) -> dict[Any, list[str]]:
    """``{contact_id: [platform, ...]}`` for a page of contacts, in one query.

    The list's channel-icon column. A property on ``Contact`` would be one query
    per row, and ``prefetch_related`` would pull whole identity rows to read one
    column off each — this reads the two columns it needs for the page's ids.
    """
    model = _identity_model()
    if model is None:
        return {}
    ids = [contact.pk for contact in contacts]
    if not ids:
        return {}
    found: dict[Any, list[str]] = {}
    rows = (
        model.objects.for_workspace(workspace)
        .filter(contact_id__in=ids)
        .values_list("contact_id", "platform")
        .order_by("platform")
    )
    for contact_id, platform in rows:
        seen = found.setdefault(contact_id, [])
        # A workspace can run two Telegram bots, so one contact legitimately has
        # two Telegram identities — and two identical icons in a table cell is
        # noise, not information.
        if platform not in seen:
            seen.append(platform)
    return found


def annotate_reachability(contacts: QuerySet[Contact], workspace: Any) -> QuerySet[Contact]:
    """Add ``has_opt_in`` and ``has_opted_out`` booleans to a contact queryset.

    Two annotations rather than one three-valued column, because they are two
    independent facts: a contact can have consented on Telegram and opted out on
    SMS, and collapsing that into a single pill would have to pick which one to
    lie about.

    Both subqueries carry ``.for_workspace(...)`` — see the module docstring on
    why the scoping guard cannot help inside ``Exists()``.
    """
    model = _identity_model()
    if model is None:
        return contacts
    scoped = model.objects.for_workspace(workspace).filter(contact=OuterRef("pk"))
    return contacts.annotate(
        has_opt_in=Exists(scoped.filter(opt_in=True, opted_out_at__isnull=True)),
        has_opted_out=Exists(scoped.filter(opted_out_at__isnull=False)),
    )


def avatar_url(channels: list[ContactChannel]) -> str:
    """The first usable profile picture across a contact's identities, or "".

    Validated before it is returned, not at the template: ``extra`` is whatever a
    platform sent, so a ``javascript:`` URL reaching an ``img`` ``src`` is a
    stored-XSS vector and a ``data:`` one is a phishing canvas
    (SECURITY-BASELINE §2). A URL that does not parse is not a URL.
    """
    for channel in channels:
        extra = channel.identity.extra if isinstance(channel.identity.extra, dict) else {}
        candidate = extra.get("profile_pic_url")
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            parts = urlsplit(candidate)
        except ValueError:
            continue
        if parts.scheme.lower() in SAFE_URL_SCHEMES and parts.netloc:
            return candidate
    return ""


def identity_for(contact: Contact, identity_id: Any) -> Any:
    """One of this contact's identities by id, or ``None``.

    Scoped by workspace *and* by contact, so an identity id belonging to another
    person in the same workspace is a miss rather than an opt-out applied to the
    wrong contact — the URL nests them, and a nested id that is not checked
    against its parent is the classic version of that bug.
    """
    model = _identity_model()
    if model is None:
        return None
    try:
        return model.objects.for_workspace(contact.workspace_id).filter(pk=identity_id, contact=contact).first()
    except (ValidationError, ValueError, TypeError):
        # A malformed uuid is a miss, not a 500 — the same reading
        # ``get_scoped_object_or_404`` takes.
        return None


def opt_out(identity: Any, *, source: str = "manual") -> bool:
    """Withdraw consent on one identity. ``True`` when it was not already out.

    Straight through to the messaging facade, which owns the audit. This
    function exists so ``views.py`` imports one module rather than two, and so
    the "reads and writes messaging only through its facade" property is
    checkable by reading this file.
    """
    from apps.messaging.services import record_opt_out

    return record_opt_out(identity, source=source)


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def recent_messages(contact: Contact, limit: int = RECENT_MESSAGE_LIMIT) -> list[MessagePreview]:
    """The contact's most recent messages across every conversation, newest first.

    Internal notes are included: they are part of what happened to this contact
    from the team's point of view, and the pane marks them. What it must never do
    is *send* one — that is the inbox's business (issue #14), and this pane has
    no compose box at all.
    """
    model = _message_model()
    if model is None:
        return []
    rows = (
        model.objects.for_workspace(contact.workspace_id)
        .filter(conversation__contact=contact)
        .select_related("conversation", "conversation__channel_connection")
        .order_by("-created_at", "-id")[:limit]
    )
    return [_as_preview(row) for row in rows]


def _as_preview(message: Any) -> MessagePreview:
    # Local import: only reachable once _message_model() has confirmed the app
    # is installed, which is the whole point of routing through installed_model.
    from apps.messaging.models import MessageDirection

    body = message.body if isinstance(message.body, dict) else {}
    raw_blocks = body.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    text = ""
    attachments = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and not text:
            # str(): body is jsonb holding whatever a platform sent, so "text"
            # can arrive as a number or an object.
            text = str(block.get("text") or "")
        elif block.get("type") != "text":
            attachments += 1
    return MessagePreview(
        message=message,
        conversation=message.conversation,
        inbound=message.direction == MessageDirection.IN,
        # Trimmed for a preview row, and escaped at render like every other
        # piece of inbound content. Never marked safe anywhere.
        text=text[:280],
        attachment_count=attachments,
        created_at=message.created_at,
    )


def live_execution(contact: Contact) -> Any:
    """The flow execution currently running for this contact, or ``None``.

    SPEC §22 allows exactly one live execution per contact across every flow, so
    "the" is accurate rather than a simplification — and if a second ever
    appeared, showing the most recent is the honest answer to "what is running
    now".
    """
    model = _execution_model()
    if model is None:
        return None
    from apps.flows.models import LIVE_STATUSES

    return (
        model.objects.for_workspace(contact.workspace_id)
        .filter(contact=contact, status__in=sorted(LIVE_STATUSES))
        .select_related("flow", "flow_version")
        .order_by("-updated_at")
        .first()
    )


def startable_flows(workspace: Any) -> QuerySet[Any]:
    """Flows an operator may start by hand: the ones with a published version.

    A draft has nothing to run — ``start_flow`` raises ``FlowNotRunnableError``
    for one — so offering it in the picker would be offering an error message.
    Draft previews are issue #12's "test on Telegram", which runs an explicit
    version and is a different affordance in a different place.
    """
    from apps.flows.models import Flow, FlowStatus

    return (
        Flow.objects.for_workspace(workspace)
        .exclude(status=FlowStatus.ARCHIVED)
        .filter(Q(versions__published=True))
        .distinct()
        .order_by("name")
    )


def startable_flow(workspace: Any, flow_id: Any) -> Any:
    """One flow an operator may start by hand, by id, or ``None``.

    Resolved out of :func:`startable_flows` rather than by primary key alone, so
    an archived or never-published flow answers the same "no such flow" a made-up
    id does. Offering a picker and then refusing what it contains is a worse
    experience than the picker simply being right.
    """
    if not flow_id:
        return None
    try:
        return startable_flows(workspace).filter(pk=flow_id).first()
    except (ValidationError, ValueError, TypeError):
        return None


def start_flow_for(contact: Contact, flow: Any, *, actor: Any = None) -> Any:
    """Start ``flow`` for ``contact`` by hand (SPEC §5's ``started_by``).

    ``manual`` is already in ``StartedBy.KINDS``, and the actor's id rides along
    so "who started this?" is answerable from the execution row rather than only
    from a log line. Starting supersedes whatever the contact was running, which
    is the engine's rule and not this call's to soften.
    """
    from apps.flows.engine import start_flow
    from apps.flows.models import StartedBy

    return start_flow(
        contact,
        flow,
        started_by=StartedBy.stamp(StartedBy.MANUAL, getattr(actor, "pk", None)),
    )


def stop_automation(contact: Contact) -> int:
    """Expire whatever this contact is running. Returns how many runs stopped.

    Delegates to the engine, which expires the executions **and** cancels the
    queue rows that would have resumed them. See the module docstring.
    """
    if _execution_model() is None:
        return 0
    from apps.flows.engine import stop_automation as engine_stop

    return engine_stop(contact)
