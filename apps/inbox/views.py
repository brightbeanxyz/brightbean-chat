"""The inbox: conversation list, thread, agent reply, assignment, pause (SPEC §14).

Two rules shape almost every function here.

**Reads are ``use_inbox``, writes are ``reply_in_inbox``.** SPEC §4.2 gives a
Viewer ``use_inbox`` and nothing else, so a Viewer sees the whole inbox and can
change none of it; ``reply_in_inbox`` is held by agent, editor and admin, which
is exactly the "Agent+" the issue asks for on assignment. No new permission key.

**Messaging state is mutated only through** :mod:`apps.messaging.services`
(ROADMAP contract 1). ``send_as_agent`` already applies the +30 minute
automation pause and already has an ``internal=True`` note path; neither is
re-implemented here, and the 30 minutes is imported as
``AGENT_AUTOMATION_PAUSE`` rather than written down a second time. A compliance
refusal is not an exception — the facade returns a ``Message`` with
``status=failed`` and a machine code, and this module turns that code into a
sentence with ``apps.messaging.codes.describe``.

**Polling.** ``rows`` and ``messages`` are the two endpoints the page polls
every three seconds, and both go through :func:`apps.common.polling.conditional`
so an unchanged poll is a 304 with no body. Nothing else on this page is
conditional: a mutation's response is never cacheable and never asks.
"""

import logging
import uuid
from dataclasses import replace
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from apps.channels.capabilities import capabilities_for
from apps.channels.events import MediaBlock, OutboundMessage, TextBlock
from apps.channels.media import MEDIA_CACHE_CONTROL, MediaUnavailableError, fetch_media, media_response
from apps.channels.models import ChannelConnection
from apps.common.htmx import toast_response
from apps.common.platforms import Platform
from apps.common.polling import conditional, if_none_match, version_etag
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import services as contact_services
from apps.contacts.builder import builder_config
from apps.contacts.conditions import ConditionValidationError
from apps.contacts.models import CustomField, Tag
from apps.inbox import rules as rules_engine
from apps.inbox import selectors, services
from apps.inbox.models import (
    DEFAULT_LABEL_COLOR,
    MAX_REMINDER_NOTE_CHARS,
    ConversationLabel,
    InboxReminder,
    InboxRule,
    ScheduledReply,
)
from apps.inbox.rendering import (
    failed_scheduled_reply,
    is_redacted,
    label_chip,
    label_chips,
    pending_reminder,
    pending_scheduled_reply,
    preview_of,
    render_message,
    rule_summary,
)
from apps.media_library.resolution import MediaNotFoundError, resolve
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest
from apps.messaging import services as messaging
from apps.messaging.codes import describe
from apps.messaging.compliance import Allowed, NeedsTag, can_send
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    ConversationState,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
)
from apps.messaging.rendering import outbound_from_body

logger = logging.getLogger(__name__)

__all__ = [
    "assign",
    "composer",
    "header",
    "inbox",
    "media",
    "messages",
    "pause",
    "retry",
    "rows",
    "send",
    "set_state",
    "sidebar",
    "stop_automation",
    "tags",
    "thread",
    "note",
]

#: The longest reply the compose box accepts. Well under
#: ``apps.messaging.ingest.MAX_TEXT_CHARS`` (which bounds what a *platform* may
#: send us): every real platform's own limit is lower than this, and a stray
#: paste of a megabyte should be refused here rather than at the adapter.
MAX_REPLY_CHARS = 8000


# --- shared context ---------------------------------------------------------


def _is_htmx(request: WorkspaceRequest) -> bool:
    """There is no django-htmx here; the library is vendored JS."""
    return request.headers.get("HX-Request") == "true"


def _can_reply(request: WorkspaceRequest) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("reply_in_inbox", False))


def _can_edit_contact(request: WorkspaceRequest) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("edit_contact_fields", False))


def _filters(request: WorkspaceRequest) -> dict[str, str]:
    """The list's filter state, normalised.

    Normalised rather than passed through, because these values are part of the
    ETag: ``?state=open`` and ``?state=open&`` must produce the same token or a
    poll would look like a change on every other request.
    """
    state = (request.GET.get("state") or "").strip()
    return {
        "state": state if state in ConversationState.values else "",
        "connection": (request.GET.get("connection") or "").strip(),
        "assignee": (request.GET.get("assignee") or "").strip(),
        "label": (request.GET.get("label") or "").strip(),
    }


def _conversation(request: WorkspaceRequest, conversation_id: Any) -> Conversation:
    """One thread of this workspace, or 404 — never 403 (SECURITY-BASELINE §1)."""
    return get_scoped_object_or_404(Conversation, request.workspace, pk=conversation_id)


def _rows_context(request: WorkspaceRequest) -> tuple[dict[str, Any], Any]:
    filters = _filters(request)
    rows = selectors.conversations_for(
        request.workspace,
        viewer=request.user,
        state=filters["state"],
        connection_id=filters["connection"] or None,
        assignee=filters["assignee"],
        label=filters["label"],
    )
    return filters, rows


#: How many chips one row prints before it says "+2".
#:
#: A layout number with a payload consequence: ``test_hostile_content`` caps the
#: whole rendered list at 20 kB, and a hundred rows carrying twenty chips each is
#: the shape that trips it.
LIST_CHIPS = 3


def _rendered_rows(request: WorkspaceRequest, rows: Any) -> dict[str, Any]:
    conversations = list(rows[: selectors.LIST_LIMIT])
    latest = selectors.last_messages_by_conversation(request.workspace, conversations)
    # Two more bounded queries for the whole page, in the shape of the one above.
    # Not prefetch_related: these rows are already sliced and materialised, and a
    # prefetch on a sliced queryset re-runs the slice as a subquery.
    labels = selectors.labels_by_conversation(request.workspace, conversations)
    pending = selectors.conversations_with_pending(request.workspace, conversations)
    return {
        "conversations": [
            {
                "conversation": conversation,
                "preview": preview_of(latest[conversation.pk]) if conversation.pk in latest else "",
                "last_internal": bool(latest[conversation.pk].internal) if conversation.pk in latest else False,
                "unread": bool(getattr(conversation, "unread", False)),
                "labels": label_chips(labels.get(conversation.pk, [])[:LIST_CHIPS]),
                "extra_labels": max(0, len(labels.get(conversation.pk, [])) - LIST_CHIPS),
                "has_pending": conversation.pk in pending,
            }
            for conversation in conversations
        ],
        "open_conversation_id": request.GET.get("open", ""),
    }


def _connections(request: WorkspaceRequest) -> list[ChannelConnection]:
    return list(ChannelConnection.objects.for_workspace(request.workspace).order_by("platform", "display_name"))


def _assignee_options(request: WorkspaceRequest) -> list[dict[str, str]]:
    """Every member of this workspace, plus the two pseudo-filters.

    Built from ``WorkspaceMembership`` rather than from the assignees already on
    conversations, so a thread can be handed to somebody who has never had one.
    """
    members = (
        WorkspaceMembership.objects.filter(workspace=request.workspace)
        .select_related("user")
        .order_by("user__name", "user__email")
    )
    return [
        {"value": selectors.ASSIGNEE_ME, "label": "Assigned to me"},
        {"value": selectors.ASSIGNEE_UNASSIGNED, "label": "Unassigned"},
        *({"value": str(m.user_id), "label": m.user.display_name} for m in members),
    ]


def _label_context_for(request: WorkspaceRequest, conversation: Conversation) -> dict[str, Any]:
    """The header's chips and its picker.

    One query for the thread's own labels, plus the palette the picker offers.
    Not folded into ``_thread_body_context``: the picker is a ``<select>``, and
    the header exists precisely because a three-second swap would close one under
    the reader — the same argument its docstring makes about the assignee
    control.
    """
    carried = selectors.labels_by_conversation(request.workspace, [conversation]).get(conversation.pk, [])
    carried_ids = {label.pk for label in carried}
    return {
        "thread_labels": label_chips(carried),
        # Deliberately **not** ``label_options``: the filter bar owns that name
        # and means the whole palette by it, and the full-page thread render
        # merges both contexts — so sharing the key silently gave the header the
        # unfiltered list and offered labels the thread already carries.
        "thread_label_options": [
            {"value": str(row.pk), "label": row.name}
            for row in selectors.labels_for(request.workspace)
            if row.pk not in carried_ids
        ],
    }


def _label_options(request: WorkspaceRequest) -> list[dict[str, str]]:
    """The filter bar's label picker.

    ``ui_select`` renders a fixed option list at template-render time, so this is
    the whole palette rather than only the labels currently in use — filtering to
    a label nothing carries yet should show an empty list, not hide the option.
    """
    return [{"value": str(row.pk), "label": row.name} for row in selectors.labels_for(request.workspace)]


def _membership(request: WorkspaceRequest, user_id: str) -> WorkspaceMembership | None:
    """The membership for ``user_id`` in *this* workspace, or None.

    Parsed before it reaches the query: a non-UUID handed to a UUID column
    raises, which would turn a mangled form post into a 500. An unparseable id
    and an id belonging to somebody outside the workspace get the same answer,
    which is also the answer the caller renders.
    """
    try:
        parsed = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        WorkspaceMembership.objects.filter(workspace=request.workspace, user_id=parsed).select_related("user").first()
    )


def _members(request: WorkspaceRequest) -> list[Any]:
    return list(
        WorkspaceMembership.objects.filter(workspace=request.workspace)
        .select_related("user")
        .order_by("user__name", "user__email")
    )


def _identity_for(request: WorkspaceRequest, conversation: Conversation) -> ContactChannelIdentity | None:
    """The identity a reply would go to, or None if the contact has no address here."""
    return (
        ContactChannelIdentity.objects.for_workspace(request.workspace)
        .filter(contact_id=conversation.contact_id, channel_connection_id=conversation.channel_connection_id)
        .first()
    )


def _compliance(request: WorkspaceRequest, conversation: Conversation) -> dict[str, Any]:
    """What the send box should say before anybody types anything.

    A pre-flight only. The facade re-decides on the way out, which is the
    decision that counts — this exists so an agent is not told "sorry" *after*
    composing a reply. There is no platform branching here or in the template:
    ``can_send`` returns a decision object and ``describe`` returns its sentence,
    both derived from ``apps.channels.policy`` as data.
    """
    identity = _identity_for(request, conversation)
    if identity is None:
        from apps.messaging.codes import Denial

        return {"allowed": False, "code": Denial.NO_IDENTITY.value, "reason": describe(Denial.NO_IDENTITY.value)}
    decision = can_send(identity, "agent", OutboundMessage())
    context: dict[str, Any] = {
        "allowed": isinstance(decision, Allowed),
        "code": decision.code,
        "reason": describe(decision.code),
        "identity": identity,
    }
    if isinstance(decision, Allowed):
        context["tag"] = decision.tag or ""
    if isinstance(decision, NeedsTag):
        # SPEC §6.4: Meta's allowed-use text is shown verbatim, so it lives in
        # the policy table and is passed straight through.
        context["allowed_use_text"] = decision.allowed_use_text
        context["allowed_tags"] = list(decision.allowed_tags)
    return context


def _contact_url(request: WorkspaceRequest, conversation: Conversation) -> str:
    """Where "open contact" goes.

    ``contacts:detail`` is issue #13's and has not landed. Reversing inside a
    guard rather than hard-coding the path means the link upgrades itself the
    day that view exists, with no edit here — the same "answer 'not yet' rather
    than raise" shape ``apps.flows.compat.installed_model`` uses for models.
    """
    try:
        return reverse(
            "contacts:detail",
            kwargs={"workspace_id": request.workspace.id, "contact_id": conversation.contact_id},
        )
    except NoReverseMatch:
        return reverse("contacts:list", kwargs={"workspace_id": request.workspace.id})


def _thread_body_context(
    request: WorkspaceRequest,
    conversation: Conversation,
    *,
    compliance: dict[str, Any],
    limit: int,
    deferred: dict[str, Any],
) -> dict[str, Any]:
    """``compliance``, ``limit`` and ``deferred`` are passed in, not derived.

    All three are part of the ETag :func:`messages` computes before deciding
    whether to render at all, and a value that decides the ETag must be the same
    value the render uses — recomputing here would be a second chance to
    disagree, and for ``deferred`` it would also be three more queries per poll.
    """
    page, has_more = selectors.thread_messages(request.workspace, conversation, limit=limit)
    return {
        "conversation": conversation,
        "rendered": [render_message(message) for message in page],
        "has_more": has_more,
        "limit": limit,
        "next_limit": min(limit + selectors.PAGE_SIZE, selectors.MAX_THREAD_MESSAGES),
        "paused_until": conversation.automation_paused_until,
        "is_paused": _is_paused(conversation),
        "compliance": compliance,
        "can_reply": _can_reply(request),
        "retry_token": uuid.uuid4().hex,
        **deferred,
    }


def _deferred_context(request: WorkspaceRequest, conversation: Conversation) -> dict[str, Any]:
    """Reminders and scheduled replies, as the thread shows them.

    Inside the polled region on purpose, unlike the compose box: these carry a
    countdown, and the whole reason the thread token folds in their rendered
    strings is so the countdown moves. Nothing here holds unsaved work.
    """
    return {
        "pending_reminders": [
            pending_reminder(row, viewer=request.user)
            for row in selectors.pending_reminders_for(request.workspace, conversation)
        ],
        "pending_replies": [
            pending_scheduled_reply(row) for row in selectors.pending_replies_for(request.workspace, conversation)
        ],
        "failed_replies": [
            failed_scheduled_reply(row) for row in selectors.failed_replies_for(request.workspace, conversation)
        ],
    }


def _countdowns(deferred: dict[str, Any]) -> tuple[str, ...]:
    """The relative strings the thread prints for its deferred work.

    Hashed rather than the timestamps behind them, deliberately: "in 20 minutes"
    changes about once a minute while ``remind_at`` never moves at all, so this
    refreshes the pane when the text would actually differ instead of on every
    three-second poll. It is the same trade ``selectors.list_version`` makes with
    ``timesince`` for the row's own timestamp.
    """
    return tuple(row.due_in for row in (*deferred["pending_reminders"], *deferred["pending_replies"]))


def _window_limit(request: WorkspaceRequest) -> int:
    """How much of the history to show, from the ``limit`` the last render emitted.

    Clamped at both ends. The floor is one page; the ceiling bounds the response
    a reader can ask for by clicking, and bounds what a query string can ask for
    without clicking at all — this endpoint is polled, so an unbounded ``limit``
    would be an unbounded render every three seconds.
    """
    raw = (request.GET.get("limit") or "").strip()
    try:
        wanted = int(raw)
    except ValueError:
        return selectors.PAGE_SIZE
    return max(selectors.PAGE_SIZE, min(wanted, selectors.MAX_THREAD_MESSAGES))


def _composer_context(conversation: Conversation) -> dict[str, Any]:
    """A fresh compose box.

    ``pause_minutes`` is read off ``AGENT_AUTOMATION_PAUSE`` rather than typed
    into the template. SPEC §14 calls the thirty minutes "a constant,
    ws-configurable later", and the day it becomes configurable the sentence
    under the send button should not be the one place still claiming thirty.
    """
    capabilities = _capabilities_for(conversation)
    return {
        "conversation": conversation,
        "compose_token": uuid.uuid4().hex,
        "max_chars": MAX_REPLY_CHARS,
        "pause_minutes": int(messaging.AGENT_AUTOMATION_PAUSE.total_seconds() // 60),
        # SPEC §14's attachment button, offered only where the platform can
        # carry one — read from the capability table rather than branched on the
        # platform, so a channel added later needs no edit here or in the
        # template. An unknown platform (no table entry) offers nothing rather
        # than everything.
        "media_kinds": sorted(
            kind
            for kind in ("image", "audio", "video", "file")
            if capabilities is not None and capabilities.supports_block(kind)
        ),
        # The picker's own endpoint (#16). Gated on workspace membership rather
        # than ``manage_media``, deliberately, so an Agent who cannot upload can
        # still attach something already in the library.
        "media_picker_url": reverse("media:picker", kwargs={"workspace_id": conversation.workspace_id}),
        "max_attachments": MAX_ATTACHMENTS,
    }


def _is_paused(conversation: Conversation) -> bool:
    until = conversation.automation_paused_until
    return bool(until and until > timezone.now())


def _custom_fields(request: WorkspaceRequest, contact: Any) -> list[dict[str, Any]]:
    """This contact's custom-field values, paired with their definitions.

    Joined here rather than in the template: ``field_values_for`` is keyed by
    field id, and looking a definition up by key is not something Django's
    template language does without a filter — and a ``dict_get`` filter added to
    the shared tag library for one panel is a worse trade than four lines here.
    """
    values = contact_services.field_values_for(contact)
    if not values:
        return []
    definitions = {field.pk: field for field in CustomField.objects.for_workspace(request.workspace)}
    return [
        {"name": definitions[field_id].name, "value": value}
        for field_id, value in values.items()
        if field_id in definitions
    ]


def _sidebar_context(request: WorkspaceRequest, conversation: Conversation) -> dict[str, Any]:
    contact = conversation.contact
    identities = list(
        ContactChannelIdentity.objects.for_workspace(request.workspace)
        .filter(contact=contact)
        .order_by("platform", "platform_user_id")
    )
    contact_tags = list(contact.tags.all())
    chosen = [tag.pk for tag in contact_tags]
    return {
        "conversation": conversation,
        "contact": contact,
        "contact_url": _contact_url(request, conversation),
        "identities": identities,
        "custom_fields": _custom_fields(request, contact),
        "contact_tags": contact_tags,
        "available_tags": list(Tag.objects.for_workspace(request.workspace).exclude(pk__in=chosen)),
        "execution": selectors.live_execution_for(request.workspace, contact),
        # The panel's pause toggle reads this. It used to arrive only because
        # the thread merged _thread_body_context over the top, so the standalone
        # sidebar endpoint — the one every refresh after a send or a pause goes
        # through — rendered "Pause automation" at a conversation that was
        # already paused.
        "is_paused": _is_paused(conversation),
        "can_reply": _can_reply(request),
        "can_edit_contact": _can_edit_contact(request),
        "members": _members(request),
    }


# --- pages and polled partials ----------------------------------------------


@login_required
@require_permission("use_inbox")
@require_GET
def inbox(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The two-pane shell. The panes fill themselves over HTMX."""
    filters, rows = _rows_context(request)
    context = {
        **filters,
        **_rendered_rows(request, rows),
        "connections": _connections(request),
        "state_options": list(ConversationState.choices),
        "assignee_options": _assignee_options(request),
        "label_options": _label_options(request),
        "can_reply": _can_reply(request),
    }
    return render(request, "inbox/list.html", context)


@login_required
@require_permission("use_inbox")
@require_GET
def rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The conversation list, polled every 3 s. 304 when nothing moved."""
    filters, queryset = _rows_context(request)
    # The payload first, the token from it second. The rows are two bounded
    # queries and the template is the expensive half, so an unchanged poll still
    # skips the work that matters — and the token cannot disagree with the
    # markup, because it is made of it. See selectors.list_version.
    context = _rendered_rows(request, queryset)
    etag = version_etag(
        "inbox-rows",
        request.user.pk,
        filters["state"],
        filters["connection"],
        filters["assignee"],
        filters["label"],
        context["open_conversation_id"],
        *selectors.list_version(context["conversations"]),
    )
    return conditional(request, etag, lambda: render(request, "inbox/_conversation_rows.html", context))


@login_required
@require_permission("use_inbox")
@require_GET
def thread(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """One conversation. The pane over HTMX, the whole page on a deep link.

    Opening a conversation marks it read. A GET with a side effect is unusual
    and deliberate here: the only row it writes is the caller's own read
    receipt, which is the literal meaning of the request. It is not conditional
    for the same reason — this response is the one that *causes* the change.
    """
    conversation = _conversation(request, conversation_id)
    services.mark_read(conversation, request.user, at=timezone.now())
    context = {
        **_thread_body_context(
            request,
            conversation,
            compliance=_compliance(request, conversation),
            limit=_window_limit(request),
            deferred=_deferred_context(request, conversation),
        ),
        **_sidebar_context(request, conversation),
        **_composer_context(conversation),
        # ``_sidebar_context`` above already carries ``members``, which the
        # header's assignee <select> reads too.
        **_label_context_for(request, conversation),
    }
    if _is_htmx(request):
        return render(request, "inbox/_thread.html", context)
    filters, queryset = _rows_context(request)
    context.update(
        {
            **filters,
            **_rendered_rows(request, queryset),
            "open_conversation_id": str(conversation.pk),
            "connections": _connections(request),
            "state_options": list(ConversationState.choices),
            "assignee_options": _assignee_options(request),
            "label_options": _label_options(request),
        }
    )
    return render(request, "inbox/list.html", context)


@login_required
@require_permission("use_inbox")
@require_GET
def messages(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """The thread's polled region: pause banner, compliance notice, history.

    The compose box is deliberately *outside* this partial. It is the one
    element on the page holding unsaved work, and a three-second poll that
    swapped it would delete whatever the agent was halfway through typing.

    The read cursor advances only on a 200. Doing it on every poll would rewrite
    the row — and therefore the list's ETag — three times a second for every
    open tab, turning a conditional GET into a change notification for itself.
    """
    conversation = _conversation(request, conversation_id)
    limit = _window_limit(request)
    # Both of these are derived from the clock, and neither writes anything when
    # it changes: a pause lapses and a messaging window closes purely by time
    # passing. A version token built only from row state would answer 304 for
    # ever after, leaving the banner insisting automation is paused and the
    # compliance notice telling an agent they may reply to somebody who has
    # since opted out. Folding the *decisions* into the token costs one indexed
    # identity read per poll and is exact — the tag changes on the tick the
    # answer does, rather than on a timer that guesses.
    compliance = _compliance(request, conversation)
    deferred = _deferred_context(request, conversation)
    etag = version_etag(
        "inbox-thread",
        request.user.pk,
        limit,
        _can_reply(request),
        _is_paused(conversation),
        compliance["code"],
        *selectors.conversation_version(request.workspace, conversation),
        # The deferred-work half. Two aggregates rather than a Max alone, for the
        # reason selectors' docstring gives: cancelling moves updated_at, and the
        # count catches what a Max cannot see.
        *selectors.deferred_version(request.workspace, conversation),
        # ...and the countdowns *as rendered*. They re-derive from the clock and
        # write nothing when they change, which is the same class of state the
        # pause banner above is in — a token built from rows alone would answer
        # 304 for ever while the thread went on saying "in 20 minutes".
        *_countdowns(deferred),
    )

    def build() -> HttpResponse:
        services.mark_read(conversation, request.user, at=timezone.now())
        context = _thread_body_context(request, conversation, compliance=compliance, limit=limit, deferred=deferred)
        return render(request, "inbox/_thread_body.html", context)

    return conditional(request, etag, build)


@login_required
@require_permission("use_inbox")
@require_GET
def composer(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """A fresh compose box, with a fresh idempotency token."""
    conversation = _conversation(request, conversation_id)
    return render(
        request,
        "inbox/_composer.html",
        {
            "can_reply": _can_reply(request),
            # The reminder form's recipient picker lives in this partial.
            "members": _members(request),
            **_composer_context(conversation),
        },
    )


@login_required
@require_permission("use_inbox")
@require_GET
def header(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """The identity line and the state/assignee controls, after something moved."""
    conversation = _conversation(request, conversation_id)
    return render(
        request,
        "inbox/_thread_header.html",
        {
            "conversation": conversation,
            "can_reply": _can_reply(request),
            "members": _members(request),
            **_label_context_for(request, conversation),
        },
    )


@login_required
@require_permission("use_inbox")
@require_GET
def sidebar(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    conversation = _conversation(request, conversation_id)
    return render(request, "inbox/_sidebar.html", _sidebar_context(request, conversation))


# --- mutations --------------------------------------------------------------
#
# All of them answer 204 with an HX-Trigger, success or failure. htmx does not
# process HX-Trigger on a non-2xx response by default, so a 400 would swallow
# the very toast the operator needs to read — the same reasoning, and the same
# shape, as apps/contacts/views.py.


def _refresh(**extra: Any) -> dict[str, Any]:
    return {"inboxThreadChanged": True, "inboxListChanged": True, **extra}


@login_required
@require_permission("reply_in_inbox")
@require_POST
def send(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """An agent reply (SPEC §14), through the facade and nowhere else."""
    return _deliver(request, conversation_id, internal=False)


@login_required
@require_permission("reply_in_inbox")
@require_POST
def note(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """An internal note: stored as a message, never sent to anybody."""
    return _deliver(request, conversation_id, internal=True)


def _deliver(request: WorkspaceRequest, conversation_id: Any, *, internal: bool) -> HttpResponse:
    conversation = _conversation(request, conversation_id)
    dropped: set[str] = set()
    try:
        # The same assembler a scheduled reply uses, so a message composed now
        # and one composed for later are built by one piece of code — including
        # what happens to an attachment the platform cannot carry.
        body = _outbound_body(request, conversation, allow_media=not internal, dropped=dropped)
    except _ComposeError as exc:
        return toast_response(tone="error", title="Nothing to send", body=str(exc))

    message = messaging.send_as_agent(
        workspace=request.workspace,
        contact=conversation.contact,
        connection=conversation.channel_connection,
        outbound=outbound_from_body(body),
        idempotency_key=_idempotency_key(request, prefix="note" if internal else "reply"),
        internal=internal,
    )
    events = _refresh(inboxSent=True)
    if message.status == MessageStatus.FAILED:
        # Not a 400: the refusal is the answer, and the thread now holds a
        # failed row explaining it. describe() rather than message.error,
        # which is a machine code that can carry a provider suffix.
        return toast_response(tone="error", title="Not sent", body=describe(message.error), events=events)
    title = "Note added" if internal else "Reply sent"
    if dropped:
        # Sent, but not with everything the agent attached. Saying so beats a
        # success toast under a message that refers to a document the contact
        # never received.
        return toast_response(
            tone="warn",
            title=title,
            body=f"{_platform_name(conversation)} cannot carry {_listed(dropped)}, so that was left off.",
            events=events,
        )
    return toast_response(tone="success", title=title, events=events)


def _platform_name(conversation: Conversation) -> str:
    return str(conversation.channel_connection.get_platform_display())


def _listed(kinds: set[str]) -> str:
    """ "an image", or "an image or a video" — for a sentence, not a log line."""
    words = sorted(f"{'an' if kind[0] in 'aeiou' else 'a'} {kind}" for kind in kinds)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _idempotency_key(request: WorkspaceRequest, *, prefix: str) -> str:
    """A stable key for one composed message (SPEC §9.4).

    The token is rendered into the form, so the browser's own double-submit —
    a second click, a refresh, an over-eager retry — arrives carrying the key
    the first one used and collapses on ``message_unique_conv_idem`` instead of
    sending twice. A missing or malformed token gets a fresh one rather than an
    error: refusing to send an agent's reply because a hidden field went astray
    would be a worse failure than the duplicate it prevents.
    """
    raw = (request.POST.get("token") or "").strip()
    try:
        token = uuid.UUID(hex=raw).hex
    except (ValueError, AttributeError, TypeError):
        token = uuid.uuid4().hex
    return f"inbox:{prefix}:{token}"


@login_required
@require_permission("reply_in_inbox")
@require_POST
def retry(request: WorkspaceRequest, workspace_id: str, conversation_id: str, message_id: str) -> HttpResponse:
    """Ask for a failed send again.

    A re-send through the facade rather than
    ``apps.messaging.handlers.schedule_send_retry``. That function walks SPEC
    §9.5's backoff ladder against ``Message.send_attempts``, which is the right
    answer for a *provider* failure and is already armed automatically for one.
    The rows a human presses this button on are mostly the other kind — a
    compliance refusal, where no provider call ever happened, the attempt budget
    is untouched and there is nothing to reschedule. Re-sending runs compliance
    again, which is exactly what should decide it when a messaging window has
    reopened since.
    """
    conversation = _conversation(request, conversation_id)
    original = get_scoped_object_or_404(Message, request.workspace, pk=message_id, conversation=conversation)
    if (
        original.status != MessageStatus.FAILED
        or original.internal
        or original.direction != MessageDirection.OUT
        # An agent's own send, and nothing else. Without this a failed
        # broadcast or automation send could be pressed through here and go out
        # as source="agent" — where the broadcast gate does not apply and the
        # human-agent allowance does. That is not a retry, it is laundering a
        # send compliance already refused into one it would permit.
        or original.source != MessageSource.AGENT
    ):
        raise Http404("Only an agent's own failed send can be retried.")

    message = messaging.send_as_agent(
        workspace=request.workspace,
        contact=conversation.contact,
        connection=conversation.channel_connection,
        outbound=_recomposed(original),
        idempotency_key=_idempotency_key(request, prefix=f"retry:{original.pk}"),
    )
    events = _refresh(inboxSent=True)
    if message.status == MessageStatus.FAILED:
        return toast_response(tone="error", title="Still not sent", body=describe(message.error), events=events)
    return toast_response(tone="success", title="Sent", events=events)


@login_required
@require_permission("use_inbox")
@require_GET
def media(
    request: WorkspaceRequest, workspace_id: str, conversation_id: str, message_id: str, index: int
) -> HttpResponse:
    """Serve one inbound attachment that is stored as a platform identifier.

    ``use_inbox`` and not ``reply_in_inbox``: this is a read, and a Viewer who
    can see the message text can see its picture.

    **The identifier comes from the row, never from the request.** The URL names
    a message and a block position; this view looks the block up and takes the
    id out of it. That ordering is the whole authorisation story for the fetch
    it triggers — a request cannot name an arbitrary ``file_id`` or an arbitrary
    Twilio media resource and have this deployment's stored credentials go and
    get it. ``apps/channels/media.py`` explains why that is a better property
    than a signed URL would have been.

    Everything unresolvable is a bare 404: a message in another workspace (never
    403 — SECURITY-BASELINE §1), an index past the end, a block that is not a
    media block, and every failure the platform hands back. The reader already
    has a tombstone in the thread; the response's job is not to explain.

    **Conditional, and the 304 comes before the platform call.** Every fetch is
    a live upstream request — two of them on Telegram — held on one of the four
    request slots this deployment ships with. The bytes behind a given
    ``(message, block index)`` never change (an inbound row is not rewritten),
    so the ETag is stable and a revalidation can be answered without resolving
    anything at all. ``apps.common.polling.conditional`` is deliberately *not*
    reused: it forces ``Cache-Control: no-store``, which is right for a poller
    driven by JavaScript and wrong for an ``<img>`` that should sit in the
    browser cache — so this composes the same two helpers under its own policy.
    """
    conversation = _conversation(request, conversation_id)
    message = get_scoped_object_or_404(Message, request.workspace, pk=message_id, conversation=conversation)

    body = message.body if isinstance(message.body, dict) else {}
    # SPEC §6.3: a message the platform asked us to retract. ``redacted_body()``
    # empties ``blocks``, so the ordinary deletion path would 404 below anyway —
    # but ``is_redacted`` deliberately accepts *either* signal, and a row that
    # kept its body while gaining the status is a state its docstring says to
    # expect. For that row the thread already says "This message was deleted",
    # and serving its picture from a URL somebody still has in their history
    # would make the retraction cosmetic.
    if is_redacted(message, body):
        raise Http404("That message was deleted.")
    blocks = body.get("blocks")
    if not isinstance(blocks, list) or not 0 <= index < len(blocks):
        raise Http404("No such block on this message.")
    block = blocks[index]
    if not isinstance(block, dict) or block.get("type") != "media":
        raise Http404("That block is not resolvable media.")
    # ``body`` is jsonb, so its shape is a claim rather than a guarantee — a row
    # written before this block type existed, or by a future ingest, can hold
    # anything. ``fetch_media`` checks again; this check is what keeps the call
    # honestly typed rather than passing it whatever the column held.
    media_id = block.get("media_id")
    if not isinstance(media_id, str):
        raise Http404("That block carries no identifier.")

    # The identifier itself, not just the position: a row rewritten to point at
    # different media (nothing does this today) must not be served from a cache
    # keyed on a tag that did not move.
    etag = version_etag("inbox-media", message.pk, index, media_id)
    if if_none_match(request, etag):
        response: HttpResponse = HttpResponseNotModified()
    else:
        try:
            resolved = fetch_media(message.channel_connection, media_id)
        except MediaUnavailableError as exc:
            # The reason is copy this deployment wrote, and it is still not
            # sent: a 404 body that varies by cause is an oracle, and the thread
            # has already told the reader as much as it can.
            logger.info("Inbox media %s[%s] is unavailable: %s", message.pk, index, exc)
            raise Http404("That attachment could not be fetched.") from exc
        response = media_response(resolved)
    # On the 304 too: RFC 9110 §15.4.5 wants the headers a 200 would have
    # carried, and a revalidation that came back without one would make the
    # browser drop the entry and re-ask on the next render.
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = MEDIA_CACHE_CONTROL
    return response


def _recomposed(original: Message) -> OutboundMessage:
    """The original's content, with its compliance decision left behind.

    ``_record`` stores the message as it went *on the wire*, so a send that
    compliance dressed in a message tag or an approved template has that tag in
    its body — and ``outbound_from_body`` reads it straight back. Replaying it
    would let the retry earn ``tag_supplied`` from a tag the agent never chose
    and that may no longer describe what they are sending, which is a Meta
    policy problem the compose box cannot create.

    Stripped rather than kept, so ``can_send`` decides the retry on what is true
    now — the window may have reopened, or the allowance may have lapsed.
    """
    return replace(outbound_from_body(original.body), tag=None, template_ref=None)


@login_required
@require_permission("reply_in_inbox")
@require_POST
def assign(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Hand a thread to a member of this workspace, or to nobody."""
    conversation = _conversation(request, conversation_id)
    raw = (request.POST.get("assignee") or "").strip()
    if not raw:
        messaging.assign_conversation(conversation, None)
        return toast_response(tone="success", title="Unassigned", events=_refresh())

    membership = _membership(request, raw)
    if membership is None:
        # Checked against this workspace's memberships, not against "is a user":
        # without it, any user id in the system could be written onto a tenant's
        # conversation by anyone holding reply_in_inbox in any workspace.
        return toast_response(
            tone="error", title="Cannot assign", body="That person is not a member of this workspace."
        )
    messaging.assign_conversation(conversation, membership.user)
    return toast_response(tone="success", title=f"Assigned to {membership.user.display_name}", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def set_state(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Open ↔ done."""
    conversation = _conversation(request, conversation_id)
    wanted = (request.POST.get("state") or "").strip()
    if wanted == ConversationState.DONE:
        messaging.close_conversation(conversation)
        return toast_response(tone="success", title="Marked done", events=_refresh())
    if wanted == ConversationState.OPEN:
        # open_conversation() is get-or-reopen, so for a thread that already
        # exists this is precisely "reopen" — and it is the facade's function
        # for it, which is the point.
        messaging.open_conversation(
            workspace=request.workspace,
            contact=conversation.contact,
            connection=conversation.channel_connection,
        )
        return toast_response(tone="success", title="Reopened", events=_refresh())
    return toast_response(tone="error", title="Unknown state", body="A conversation is either open or done.")


@login_required
@require_permission("reply_in_inbox")
@require_POST
def pause(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Pause or resume automation on this thread.

    ``AGENT_AUTOMATION_PAUSE`` is imported, never restated: SPEC §14's thirty
    minutes is one constant, and a manual pause meaning something different from
    the pause a reply applies would be a surprise nobody asked for.
    """
    conversation = _conversation(request, conversation_id)
    if (request.POST.get("action") or "").strip() == "resume":
        messaging.pause_automation(conversation, None)
        return toast_response(tone="success", title="Automation resumed", events=_refresh())
    messaging.pause_automation(conversation, timezone.now() + messaging.AGENT_AUTOMATION_PAUSE)
    return toast_response(tone="success", title="Automation paused", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def stop_automation(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Expire whatever flow is currently holding this contact.

    Through ``apps.flows.engine.stop_automation``, which takes the contact
    advisory lock and cancels the queue rows that would have woken the run back
    up. Writing ``status`` here instead would be a second write site for a
    column the runner is built around owning.

    Issue #13 needed the same call for its contact page and got there first, so
    this uses its name rather than adding a synonym beside it.
    """
    from apps.flows.engine import stop_automation

    conversation = _conversation(request, conversation_id)
    stopped = stop_automation(conversation.contact)
    if not stopped:
        return toast_response(tone="info", title="Nothing running", events=_refresh())
    return toast_response(tone="success", title="Automation stopped", events=_refresh())


@login_required
@require_permission("edit_contact_fields")
@require_POST
def tags(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Add or remove one of this workspace's tags on the contact.

    ``edit_contact_fields`` rather than ``manage_crm``, and the line is where
    the two keys already sit in SPEC §4.2: ``manage_crm`` governs the tag
    *vocabulary* — creating, renaming and deleting definitions, which stays in
    contacts' settings pages — while ``edit_contact_fields`` governs one
    contact's own values, which an agent holds. This endpoint can only apply a
    tag that already exists, so it cannot reach the vocabulary.
    """
    conversation = _conversation(request, conversation_id)
    wanted = (request.POST.get("tag") or "").strip()
    if not wanted:
        # The picker's own placeholder option. Nothing to do, and answering 404
        # for it would turn "changed my mind" into an error.
        return toast_response(tone="info", title="No tag selected")
    tag = get_scoped_object_or_404(Tag, request.workspace, pk=wanted)
    if (request.POST.get("action") or "").strip() == "remove":
        contact_services.remove_tag(conversation.contact, tag)
        return toast_response(tone="success", title="Tag removed", events={"inboxContactChanged": True})
    contact_services.add_tag(conversation.contact, tag)
    return toast_response(tone="success", title="Tag added", events={"inboxContactChanged": True})


# ---------------------------------------------------------------------------
# Labels on a thread (issue #24)
# ---------------------------------------------------------------------------


@login_required
@require_permission("reply_in_inbox")
@require_POST
def add_label(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Put a label on this thread.

    Gated on ``reply_in_inbox`` rather than ``edit_contact_fields`` — the split
    the neighbouring ``tags`` view documents. A conversation label is a property
    of the *thread*, so labelling one is inbox work; a contact tag follows the
    person across every channel they use, which is CRM work.
    """
    conversation = _conversation(request, conversation_id)
    label = _label_or_404(request, request.POST.get("label") or "")
    try:
        services.apply_label(conversation, label, by=request.user)
    except services.InboxError as exc:
        return toast_response(tone="error", title="Not labelled", body=str(exc), events=_refresh())
    return toast_response(tone="success", title="Labelled", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def remove_label(request: WorkspaceRequest, workspace_id: str, conversation_id: str, label_id: str) -> HttpResponse:
    """Take a label off this thread."""
    conversation = _conversation(request, conversation_id)
    label = _label_or_404(request, label_id)
    services.remove_label(conversation, label)
    return toast_response(tone="success", title="Label removed", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def bulk_label(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Apply or remove one label across the threads selected in the list.

    Every conversation id is resolved **inside this workspace** and anything
    else is skipped silently rather than refused: a bulk action is issued
    against a list that was rendered seconds ago, so an id that has since become
    unreachable is ordinary rather than an attack — and the count that comes back
    tells the operator what actually happened either way.
    """
    label = _label_or_404(request, request.POST.get("label") or "")
    # Bounded by what the list itself can show, stated where the post arrives
    # rather than trusted from the DOM.
    wanted = request.POST.getlist("conversation")[: selectors.LIST_LIMIT]
    conversations = list(
        Conversation.objects.for_workspace(request.workspace).filter(pk__in=[_uuid(v) for v in wanted if _uuid(v)])
    )
    removing = (request.POST.get("action") or "").strip() == "remove"

    changed = 0
    for conversation in conversations:
        try:
            if removing:
                changed += 1 if services.remove_label(conversation, label) else 0
            else:
                changed += 1 if services.apply_label(conversation, label, by=request.user) else 0
        except services.InboxError:
            # One thread already carrying its maximum must not stop the rest.
            continue

    verb = "removed from" if removing else "added to"
    return toast_response(
        tone="success",
        title=f"Label {verb} {changed} conversation{'' if changed == 1 else 's'}",
        events=_refresh(),
    )


# ---------------------------------------------------------------------------
# Reminders and scheduled replies (issue #24)
# ---------------------------------------------------------------------------


@login_required
@require_permission("reply_in_inbox")
@require_POST
def create_reminder(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """ "Remind me/somebody about this thread at ..." (SPEC §14)."""
    conversation = _conversation(request, conversation_id)
    when = _when(request.POST.get("remind_at"))
    if when is None:
        return toast_response(tone="error", title="No reminder set", body="Pick a date and time.")
    recipient = _membership(request, (request.POST.get("recipient") or "").strip())
    try:
        services.schedule_reminder(
            conversation,
            recipient=recipient.user if recipient else request.user,
            remind_at=when,
            note=(request.POST.get("note") or "")[:MAX_REMINDER_NOTE_CHARS],
            created_by=request.user,
            # Deliberately no compose token, unlike the scheduled reply below.
            # This endpoint answers with ``_refresh()``, which does not carry
            # ``inboxSent``, so the compose box does not refetch and its token
            # does not rotate — a token guard here would read a *deliberate*
            # second reminder from the same render as a duplicate and silently
            # return the first. The double click is handled where it happens,
            # with ``hx-disabled-elt`` on the button, and the harm if one slips
            # through is a repeated in-app nudge rather than a second message to
            # the contact.
        )
    except services.InboxError as exc:
        return toast_response(tone="error", title="No reminder set", body=str(exc))
    return toast_response(tone="success", title="Reminder set", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def cancel_reminder(
    request: WorkspaceRequest, workspace_id: str, conversation_id: str, reminder_id: str
) -> HttpResponse:
    conversation = _conversation(request, conversation_id)
    reminder = get_scoped_object_or_404(InboxReminder, request.workspace, pk=reminder_id, conversation=conversation)
    if not services.cancel_reminder(reminder):
        return toast_response(tone="info", title="Already gone", events=_refresh())
    return toast_response(tone="success", title="Reminder cancelled", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def create_scheduled_reply(request: WorkspaceRequest, workspace_id: str, conversation_id: str) -> HttpResponse:
    """Compose now, send later (SPEC §14).

    Compliance is **not** checked here. The composer shows the current verdict as
    a courtesy, but a window can close in the hours before this goes out, so the
    decision that counts is the one ``send_as_agent`` makes when the queue row
    fires — and a refusal then is surfaced rather than dropped.
    """
    conversation = _conversation(request, conversation_id)
    when = _when(request.POST.get("send_at"))
    if when is None:
        return toast_response(tone="error", title="Not scheduled", body="Pick a date and time.")
    try:
        body = _outbound_body(request, conversation)
    except _ComposeError as exc:
        return toast_response(tone="error", title="Not scheduled", body=str(exc))
    try:
        services.schedule_reply(
            conversation,
            body=body,
            send_at=when,
            created_by=request.user,
            compose_token=_compose_token(request),
        )
    except services.InboxError as exc:
        return toast_response(tone="error", title="Not scheduled", body=str(exc))
    return toast_response(tone="success", title="Reply scheduled", events=_refresh(inboxSent=True))


@login_required
@require_permission("reply_in_inbox")
@require_POST
def update_scheduled_reply(
    request: WorkspaceRequest, workspace_id: str, conversation_id: str, scheduled_reply_id: str
) -> HttpResponse:
    conversation = _conversation(request, conversation_id)
    reply = get_scoped_object_or_404(
        ScheduledReply, request.workspace, pk=scheduled_reply_id, conversation=conversation
    )
    when = _when(request.POST.get("send_at")) or reply.send_at
    try:
        body = _outbound_body(request, conversation)
    except _ComposeError as exc:
        return toast_response(tone="error", title="Not changed", body=str(exc))
    try:
        services.reschedule_reply(reply, body=body, send_at=when)
    except services.InboxError as exc:
        return toast_response(tone="error", title="Not changed", body=str(exc))
    return toast_response(tone="success", title="Scheduled reply updated", events=_refresh())


@login_required
@require_permission("reply_in_inbox")
@require_POST
def cancel_scheduled_reply(
    request: WorkspaceRequest, workspace_id: str, conversation_id: str, scheduled_reply_id: str
) -> HttpResponse:
    conversation = _conversation(request, conversation_id)
    reply = get_scoped_object_or_404(
        ScheduledReply, request.workspace, pk=scheduled_reply_id, conversation=conversation
    )
    if not services.cancel_scheduled_reply(reply):
        return toast_response(tone="info", title="Already gone", events=_refresh())
    return toast_response(tone="success", title="Scheduled reply cancelled", events=_refresh())


# ---------------------------------------------------------------------------
# Helpers for the above
# ---------------------------------------------------------------------------


class _ComposeError(ValueError):
    """A composed message this view will not build."""


def _compose_token(request: WorkspaceRequest) -> str:
    """The compose box's per-render token, normalised (SPEC §9.4).

    Same field and same parsing as :func:`_idempotency_key`, which is what makes
    a double-clicked "Schedule" one queued reply rather than two messages to the
    contact. It is safe to key on **because scheduling refetches the compose
    box** — ``create_scheduled_reply`` answers with ``inboxSent``, so the next
    composition carries a fresh token and a second, deliberate scheduled reply is
    not mistaken for a duplicate.

    A missing or malformed token yields ``""``, which the services read as "no
    guard" rather than as a reason to refuse — an operator losing their reply
    because a hidden field went astray is worse than the duplicate.
    """
    raw = (request.POST.get("token") or "").strip()
    try:
        return uuid.UUID(hex=raw).hex
    except (ValueError, AttributeError, TypeError):
        return ""


def _label_or_404(request: WorkspaceRequest, label_id: str) -> ConversationLabel:
    return get_scoped_object_or_404(ConversationLabel, request.workspace, pk=label_id)


def _uuid(value: str) -> Any:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _when(raw: Any) -> Any:
    """Parse a ``datetime-local`` value into an aware datetime, or None.

    The picker posts a naive local string. Interpreted in the **current
    timezone** rather than UTC: an agent typing 4pm means four in the afternoon
    where they are, and ``TIME_ZONE`` is what the rest of the app already renders
    timestamps in.
    """
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return None
    try:
        # ``parse_datetime`` returns None for something it cannot parse at all,
        # but **raises** ``ValueError`` for a value that is well formatted and
        # not a real datetime — "2099-13-45T00:00" matches its regex and then
        # fails ``datetime(month=13)``. Uncaught, that is a 500 anyone can reach
        # from the compose box, which is the same trap ``selectors._as_uuid``
        # documents for the id filters.
        parsed = parse_datetime(text)
    except ValueError:
        return None
    if parsed is None:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _outbound_body(
    request: WorkspaceRequest,
    conversation: Conversation,
    *,
    allow_media: bool = True,
    dropped: set[str] | None = None,
) -> dict[str, Any]:
    """Turn the compose form into the stored body shape.

    ``allow_media`` is False for an internal note: a note is never sent, so a
    library attachment on one would be a delivery URL minted for nobody, sitting
    in a body the adapter will never see.

    ``OutboundMessage.to_body()`` and nothing else — the same shape
    ``Message.body`` carries, which its own docstring calls "a persisted
    contract". Storing anything else here would be a second serialisation of the
    same thing, and this one already knows how to carry media.
    """
    text = (request.POST.get("body") or "").strip()
    if len(text) > MAX_REPLY_CHARS:
        raise _ComposeError("That message is too long.")
    dropped = dropped if dropped is not None else set()
    blocks: list[Any] = []
    if text:
        blocks.append(TextBlock(text=text))
    if allow_media:
        blocks.extend(_attachments(request, conversation, dropped))
    if not blocks:
        raise _ComposeError(
            f"{conversation.channel_connection.get_platform_display()} cannot carry that attachment."
            if dropped
            else "Write something first."
        )
    return OutboundMessage(blocks=tuple(blocks)).to_body()


def _attachments(request: WorkspaceRequest, conversation: Conversation, dropped: set[str]) -> list[Any]:
    """Media blocks for the library assets the composer picked.

    Ids, never URLs. ``apps.media_library.picker``'s contract is explicit —
    "Store ``id``. Never store ``url`` — it is minted per request" — so the
    delivery URL is resolved here, at compose time, from an id checked against
    this workspace.

    What the platform cannot carry is dropped rather than refused, which is the
    same call ``apps.channels.downgrade`` makes for the automation path: an
    operator who attaches a PDF to a channel that takes only images should get
    their message rather than a wall. **Dropped kinds are reported**, through
    ``dropped``, so the caller can say so — a silent drop leaves an agent
    referring in the text to a document that was never sent.
    """
    ids = [value for value in request.POST.getlist("media")[:MAX_ATTACHMENTS] if value]
    if not ids:
        return []
    capabilities = _capabilities_for(conversation)
    blocks: list[Any] = []
    for media_id in ids:
        try:
            asset = resolve(media_id, workspace=request.workspace)
        except MediaNotFoundError:
            raise _ComposeError("One of those attachments is no longer in the library.") from None
        kind = str(asset["kind"])
        if capabilities is not None and not capabilities.supports_block(kind):
            dropped.add(kind)
            continue
        blocks.append(MediaBlock(kind=kind, url=str(asset["url"])))
    return blocks


def _capabilities_for(conversation: Conversation) -> Any:
    """This thread's platform capabilities, or None when it has no table entry.

    ``capabilities_for`` raises for an unknown platform rather than returning a
    permissive default, which is right for it and wrong here: the compose box
    must still work on a deployment carrying a platform this build does not know
    about.
    """
    try:
        return capabilities_for(conversation.channel_connection.platform)
    except KeyError:  # pragma: no cover - every platform is in the table
        return None


#: How many library assets one message may carry. ``apps.messaging.ingest`` caps
#: what a *platform* may send us at ``MAX_ATTACHMENTS``; this is the same
#: question asked of our own compose box.
MAX_ATTACHMENTS = 10


# ---------------------------------------------------------------------------
# Settings: the label palette (issue #24)
# ---------------------------------------------------------------------------
#
# Two views per surface, sharing one context builder — the shape
# ``apps/contacts/views.py``'s tag manager established: a page view, and a rows
# partial the mutations re-fetch through an ``HX-Trigger`` event. The mutations
# answer 2xx even when they refuse, because htmx drops ``HX-Trigger`` on a
# non-2xx and a refusal has to be able to say so in a toast.


def _label_context(request: WorkspaceRequest) -> dict[str, Any]:
    labels = list(selectors.labels_for(request.workspace))
    counts = selectors.label_usage(request.workspace)
    return {
        "labels": [{"label": label, "chip": label_chip(label), "used": counts.get(label.pk, 0)} for label in labels],
        "default_color": DEFAULT_LABEL_COLOR,
    }


@login_required
@require_permission("reply_in_inbox")
@require_GET
def label_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The label manager.

    ``reply_in_inbox``, not ``manage_workspace_settings``: a label is the inbox's
    own filing system and an Agent who cannot create one has to ask an Admin
    before they can file anything. The rule manager next door is the other
    call — rules act on other people's conversations.
    """
    return render(request, "inbox/label_settings.html", _label_context(request))


@login_required
@require_permission("reply_in_inbox")
@require_GET
def label_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Just the rows, for the re-fetch after a mutation."""
    return render(request, "inbox/_label_rows.html", _label_context(request))


@login_required
@require_permission("reply_in_inbox")
@require_POST
def label_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    try:
        services.create_label(
            request.workspace,
            name=request.POST.get("name") or "",
            color=request.POST.get("color") or "",
        )
    except services.InboxError as exc:
        return toast_response(tone="error", title="Not created", body=str(exc))
    return toast_response(tone="success", title="Label created", events={"inboxLabelsChanged": True})


@login_required
@require_permission("reply_in_inbox")
@require_POST
def label_update(request: WorkspaceRequest, workspace_id: str, label_id: str) -> HttpResponse:
    label = _label_or_404(request, label_id)
    try:
        services.update_label(label, name=request.POST.get("name") or "", color=request.POST.get("color") or "")
    except services.InboxError as exc:
        return toast_response(tone="error", title="Not saved", body=str(exc))
    return toast_response(tone="success", title="Label saved", events=_refresh(inboxLabelsChanged=True))


@login_required
@require_permission("reply_in_inbox")
@require_POST
def label_delete(request: WorkspaceRequest, workspace_id: str, label_id: str) -> HttpResponse:
    """Delete a label and every link to it.

    A real delete, and the cascade is the point: a label that is gone should be
    gone from the threads carrying it too. Rules naming it keep working — the
    action resolves nothing and applies nothing, and the settings list shows
    "(deleted)" where the name was, rather than the rule silently doing less
    than it says.
    """
    label = _label_or_404(request, label_id)
    label.delete()
    return toast_response(tone="success", title="Label deleted", events=_refresh(inboxLabelsChanged=True))


# ---------------------------------------------------------------------------
# Settings: the rules engine (issue #24)
# ---------------------------------------------------------------------------


def _rule_vocabulary(request: WorkspaceRequest) -> dict[str, dict[str, str]]:
    """The id → name maps a rule summary is written from, built once per page."""
    return {
        "labels": {str(row.pk): row.name for row in selectors.labels_for(request.workspace)},
        "members": {str(m.user_id): m.user.display_name for m in _members(request)},
        "connections": {str(row.pk): row.display_name for row in _connections(request)},
    }


def _rule_context(request: WorkspaceRequest) -> dict[str, Any]:
    vocabulary = _rule_vocabulary(request)
    rules = list(InboxRule.objects.for_workspace(request.workspace).order_by("priority", "name"))
    return {
        "rules": [rule_summary(rule, **vocabulary) for rule in rules],
        "rule_count": len(rules),
    }


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def rule_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The inbox-rules manager.

    ``manage_workspace_settings``, unlike the label manager: a rule reassigns and
    closes conversations across the whole workspace without anybody watching, and
    every other settings CRUD in this product sits above Agent for the same
    reason (contacts' tag editor is ``manage_crm``, triggers are ``edit_flows``,
    webhooks are this key). ``PERMISSION_KEYS`` is the whole vocabulary and this
    invents nothing.
    """
    return render(request, "inbox/rule_settings.html", _rule_context(request))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def rule_rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    return render(request, "inbox/_rule_rows.html", _rule_context(request))


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def rule_form(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The editor for one rule, new or existing.

    The contact half of the condition is rendered by
    ``templates/contacts/_filter_bar.html`` — the same builder the segment editor
    uses, driven by the same payload from
    :func:`apps.contacts.views.builder_config`. Reimplementing it here would mean
    a second operator table that drifts the day somebody adds an operator to the
    condition engine.
    """
    rule = None
    raw = (request.GET.get("rule") or "").strip()
    if raw:
        rule = get_scoped_object_or_404(InboxRule, request.workspace, pk=raw)
    document = rule.condition_json if rule and isinstance(rule.condition_json, dict) else {}
    contact_half = document.get("contact") if isinstance(document.get("contact"), dict) else {}
    return render(
        request,
        "inbox/_rule_form.html",
        {
            "rule": rule,
            "condition": document,
            "selected_actions": _selected_actions(rule),
            "filter_config": builder_config(request.workspace, document=contact_half),
            "connections": _connections(request),
            "platform_options": list(Platform.choices),
            "labels": list(selectors.labels_for(request.workspace)),
            "members": _members(request),
        },
    )


def _selected_actions(rule: Any) -> dict[str, Any]:
    """The stored actions, in the shape the three form controls need.

    Assembled here rather than reading ``actions_json`` in the template, because
    the controls are a multi-select, a single select and a checkbox — three
    different questions of one list, and Django's template language cannot ask
    any of them without a filter per control.
    """
    actions = rule.actions_json if rule and isinstance(rule.actions_json, list) else []
    verbs = [item for item in actions if isinstance(item, dict)]
    return {
        "label_ids": [str(item.get("label_id") or "") for item in verbs if item.get("type") == "add_label"],
        "assignee_id": next(
            (str(item.get("user_id") or "") for item in verbs if item.get("type") == "assign_to_member"), ""
        ),
        "mark_done": any(item.get("type") == "mark_done" for item in verbs),
    }


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def rule_save(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create or update one rule.

    One endpoint for both, because the whole document is posted either way and
    a second view would be the same forty lines with one lookup changed.
    """
    raw = (request.POST.get("rule") or "").strip()
    rule = get_scoped_object_or_404(InboxRule, request.workspace, pk=raw) if raw else None
    name = (request.POST.get("name") or "").strip()
    if not name:
        return toast_response(tone="error", title="Not saved", body="A rule needs a name.")
    try:
        condition = rules_engine.validate_condition(request.workspace, _posted_condition(request))
        actions = rules_engine.validate_actions(request.workspace, _posted_actions(request))
    except (rules_engine.RuleValidationError, ConditionValidationError) as exc:
        return toast_response(tone="error", title="Not saved", body=str(exc))

    if rule is None:
        rule = InboxRule(workspace=request.workspace, priority=_next_priority(request))
    rule.name = name[:120]
    rule.condition_json = condition
    rule.actions_json = actions
    rule.enabled = bool(request.POST.get("enabled"))
    rule.save()
    return toast_response(tone="success", title="Rule saved", events={"inboxRulesChanged": True})


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def rule_toggle(request: WorkspaceRequest, workspace_id: str, rule_id: str) -> HttpResponse:
    rule = get_scoped_object_or_404(InboxRule, request.workspace, pk=rule_id)
    rule.enabled = not rule.enabled
    rule.save(update_fields=["enabled", "updated_at"])
    title = "Rule enabled" if rule.enabled else "Rule disabled"
    return toast_response(tone="success", title=title, events={"inboxRulesChanged": True})


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def rule_delete(request: WorkspaceRequest, workspace_id: str, rule_id: str) -> HttpResponse:
    rule = get_scoped_object_or_404(InboxRule, request.workspace, pk=rule_id)
    rule.delete()
    return toast_response(tone="success", title="Rule deleted", events={"inboxRulesChanged": True})


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def rule_reorder(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Renumber the rules from the order the list posts.

    Serves both affordances: the drag handle posts the whole order it ended up
    with, and the up/down buttons post the same list with two entries swapped.
    One endpoint means the keyboard path and the pointer path cannot disagree
    about what happened.
    """
    services.reorder_rules(request.workspace, request.POST.getlist("rule"))
    return toast_response(tone="success", title="Order updated", events={"inboxRulesChanged": True})


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def rule_test(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Dry-run the posted condition against the last messages received.

    Deliberately scored with the same :func:`apps.inbox.rules.matches_shallow`
    the live hook calls, over the same :class:`~apps.inbox.rules.RuleInput` — the
    only difference is which constructor built it. Anything less than that and
    "the dry-run matches live behaviour" is a claim rather than a property.

    It writes nothing, and that is structural rather than careful: this view
    never reaches :mod:`apps.inbox.routing`, which is where applying lives.
    """
    try:
        condition = rules_engine.validate_condition(request.workspace, _posted_condition(request))
    except (rules_engine.RuleValidationError, ConditionValidationError) as exc:
        return render(request, "inbox/_rule_test.html", {"error": str(exc), "sample": 0}, status=200)
    matched, sample = selectors.dry_run(request.workspace, condition)
    return render(
        request,
        "inbox/_rule_test.html",
        {
            "matches": [
                {"message": message, "preview": preview_of(message), "conversation": message.conversation}
                for message in matched
            ],
            "sample": sample,
        },
    )


def _next_priority(request: WorkspaceRequest) -> int:
    """New rules go last. ``reorder_rules`` renumbers everything anyway."""
    from django.db.models import Max

    highest = InboxRule.objects.for_workspace(request.workspace).aggregate(top=Max("priority"))["top"]
    return 0 if highest is None else highest + services.PRIORITY_STEP


def _posted_condition(request: WorkspaceRequest) -> dict[str, Any]:
    """Assemble the three condition halves from the form.

    The contact half arrives as one ``filter`` json string, because that is what
    ``templates/contacts/_filter_bar.html`` posts — it serialises the builder's
    Alpine state into a single hidden input so the server hands the document
    straight to the condition engine rather than re-assembling it from separate
    fields, which would be a second parser for a language that already has one.
    """
    document: dict[str, Any] = {}
    channel: dict[str, Any] = {}
    if platforms := request.POST.getlist("platform"):
        channel["platforms"] = platforms
    if connections := request.POST.getlist("connection"):
        channel["connection_ids"] = connections
    if channel:
        document["channel"] = channel
    if keywords := _posted_keywords(request):
        document["keywords"] = keywords
    contact = _posted_json(request.POST.get("filter"))
    if isinstance(contact, dict) and contact.get("rules"):
        document["contact"] = contact
    return document


def _posted_keywords(request: WorkspaceRequest) -> list[dict[str, str]]:
    """Zip the two parallel lists the repeatable keyword rows post.

    A length mismatch drops the tail rather than raising, unlike
    ``apps.flows.triggers.forms``: that one is protecting a *trigger*, where a
    keyword matching words nobody configured starts a flow at a stranger. Here
    the validator that follows reports what was stored, and the rule list shows
    every keyword back, so a truncated submission is visible rather than silent.
    """
    texts = request.POST.getlist("keyword_text")
    modes = request.POST.getlist("keyword_mode")
    return [{"text": text, "mode": mode} for text, mode in zip(texts, modes, strict=False) if (text or "").strip()]


def _posted_actions(request: WorkspaceRequest) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = [
        {"type": "add_label", "label_id": value} for value in request.POST.getlist("action_label") if value
    ]
    if assignee := (request.POST.get("action_assignee") or "").strip():
        actions.append({"type": "assign_to_member", "user_id": assignee})
    if request.POST.get("action_done"):
        actions.append({"type": "mark_done"})
    return actions


def _posted_json(raw: Any) -> Any:
    """A json field from a form, or ``{}``.

    Malformed json is an empty document rather than a 500: the value is written
    by a browser and a half-serialised one is a bug report, not an exception the
    operator can act on.
    """
    import json

    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}
