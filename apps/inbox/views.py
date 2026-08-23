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

import uuid
from dataclasses import replace
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.common.htmx import toast_response
from apps.common.polling import conditional, version_etag
from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import services as contact_services
from apps.contacts.models import CustomField, Tag
from apps.inbox import selectors, services
from apps.inbox.rendering import preview_of, render_message
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

__all__ = [
    "assign",
    "composer",
    "inbox",
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
    )
    return filters, rows


def _rendered_rows(request: WorkspaceRequest, rows: Any) -> dict[str, Any]:
    conversations = list(rows[: selectors.LIST_LIMIT])
    latest = selectors.last_messages_by_conversation(request.workspace, conversations)
    return {
        "conversations": [
            {
                "conversation": conversation,
                "preview": preview_of(latest[conversation.pk]) if conversation.pk in latest else "",
                "last_internal": bool(latest[conversation.pk].internal) if conversation.pk in latest else False,
                "unread": bool(getattr(conversation, "unread", False)),
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
) -> dict[str, Any]:
    """``compliance`` and ``limit`` are passed in, not derived.

    Both are part of the ETag :func:`messages` computes before deciding whether
    to render at all, and a value that decides the ETag must be the same value
    the render uses — recomputing here would be a second chance to disagree.
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
    }


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
    return {
        "conversation": conversation,
        "compose_token": uuid.uuid4().hex,
        "max_chars": MAX_REPLY_CHARS,
        "pause_minutes": int(messaging.AGENT_AUTOMATION_PAUSE.total_seconds() // 60),
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
        "can_reply": _can_reply(request),
    }
    return render(request, "inbox/list.html", context)


@login_required
@require_permission("use_inbox")
@require_GET
def rows(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """The conversation list, polled every 3 s. 304 when nothing moved."""
    filters, queryset = _rows_context(request)
    etag = version_etag(
        "inbox-rows",
        request.user.pk,
        filters["state"],
        filters["connection"],
        filters["assignee"],
        request.GET.get("open", ""),
        *selectors.list_version(request.workspace, queryset, request.user),
    )
    return conditional(
        request,
        etag,
        lambda: render(request, "inbox/_conversation_rows.html", _rendered_rows(request, queryset)),
    )


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
        ),
        **_sidebar_context(request, conversation),
        **_composer_context(conversation),
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
    etag = version_etag(
        "inbox-thread",
        request.user.pk,
        limit,
        _can_reply(request),
        _is_paused(conversation),
        compliance["code"],
        *selectors.conversation_version(request.workspace, conversation),
    )

    def build() -> HttpResponse:
        services.mark_read(conversation, request.user, at=timezone.now())
        context = _thread_body_context(request, conversation, compliance=compliance, limit=limit)
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
        {"can_reply": _can_reply(request), **_composer_context(conversation)},
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
    text = (request.POST.get("body") or "").strip()
    if not text:
        return toast_response(tone="error", title="Nothing to send", body="Write something first.")
    if len(text) > MAX_REPLY_CHARS:
        return toast_response(
            tone="error",
            title="Too long",
            body=f"Replies are limited to {MAX_REPLY_CHARS:,} characters.",
        )

    message = messaging.send_as_agent(
        workspace=request.workspace,
        contact=conversation.contact,
        connection=conversation.channel_connection,
        outbound=OutboundMessage(blocks=(TextBlock(text=text),)),
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
    return toast_response(tone="success", title=title, events=events)


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

    Through ``apps.flows.engine.stop_executions``, which takes the contact
    advisory lock and cancels the queue rows that would have woken the run back
    up. Writing ``status`` here instead would be a second write site for a
    column the runner is built around owning.
    """
    from apps.flows.engine import stop_executions

    conversation = _conversation(request, conversation_id)
    stopped = stop_executions(conversation.contact)
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
