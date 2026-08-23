"""WhatsApp message templates: authoring, submission, review and rendering.

SPEC §6.5 gives templates two jobs and this module owns both:

    Template CRUD against the Graph API from the ``whatsapp_template`` model;
    poll status after submit.

Deliberately **not** in :mod:`apps.channels.providers.whatsapp`. That module is
the adapter — webhook shapes, wire payloads, one HTTP client — and the work here
is rows, state transitions and copy for a person. The adapter keeps a
:func:`~apps.channels.providers.whatsapp.poll_template_statuses` delegate only
because ``apps.queueing.housekeeping.OPTIONAL_JOB_PATHS`` reserved that dotted
path for this issue before either module existed.

--------------------------------------------------------------------------
Why a template exists at all
--------------------------------------------------------------------------

Outside the 24-hour window WhatsApp accepts nothing but a template Meta has
already reviewed. That gate is not implemented here and must not be:
``apps.messaging.compliance.can_send`` answers ``NeedsTemplate`` from the
``PlatformPolicy`` row alone (ROADMAP contract 4), knowing nothing about this
table. What this module supplies is the *material* that satisfies the gate.

--------------------------------------------------------------------------
Variables, and the SSTI ban
--------------------------------------------------------------------------

A template body is written by an operator and its ``{{1}}``-style slots are
filled, at send time, with values that came from a stranger — a contact's first
name, a field an External Request wrote. SECURITY-BASELINE §3 is explicit about
what that combination may not become:

    ``{{placeholder}}`` rendering is **plain token substitution** via the one
    shared renderer. User- or contact-supplied content is **never** evaluated by
    Django/Jinja template engines.

So every substitution in this module goes through
:func:`apps.flows.rendering.render`, the same function the flow engine uses, and
there is no template engine anywhere near it. Meta's ``{{1}}`` numbering is
already inside that renderer's token grammar (digits are legal token characters),
so the shared renderer needs no WhatsApp-shaped extension — a slot map of
``{"1": "Ada"}`` is all it takes.

The preview an operator sees in settings is rendered by exactly this path, so
what the preview shows and what the send produces cannot drift.
"""

import logging
import re
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.urls import reverse

from apps.channels.models import (
    ChannelConnection,
    ConnectionStatus,
    WhatsAppCostHint,
    WhatsAppTemplate,
    WhatsAppTemplateStatus,
)
from apps.channels.providers import whatsapp
from apps.channels.providers.base import BACKGROUND_TIMEOUT
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.flows.rendering import RenderContext, render

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BODY_CHARS",
    "MAX_FOOTER_CHARS",
    "MAX_HEADER_CHARS",
    "MAX_SLOTS",
    "TEMPLATE_NAME_PATTERN",
    "approved_templates_for",
    "cost_hint_for",
    "delete_template",
    "poll_pending",
    "preview",
    "refresh_status",
    "slots_for",
    "submit",
    "variable_schema",
]

#: Meta's own rule for a template name: lowercase letters, digits, underscores.
#: Enforced here as well as in the form, because ``submit`` is reachable from a
#: fixture and a name Meta rejects costs a round trip and a confusing error.
TEMPLATE_NAME_PATTERN = r"^[a-z0-9_]{1,512}$"

_NAME_RE = re.compile(TEMPLATE_NAME_PATTERN)

#: Meta's ``{{n}}`` placeholder. Deliberately narrower than the shared
#: renderer's grammar: a template slot is a *number*, and matching the wider
#: token alphabet here would report ``{{first_name}}`` as a slot the operator
#: has to supply a value for, when in a template it is simply literal text.
_SLOT_RE = re.compile(r"\{\{\s*(\d{1,3})\s*\}\}")

#: Meta's per-component character limits. Enforced before submission so an
#: operator is told in the form rather than by a Graph error.
MAX_HEADER_CHARS = 60
MAX_BODY_CHARS = 1024
MAX_FOOTER_CHARS = 60
MAX_BUTTON_TEXT_CHARS = 25

#: A cap on how many variables one template may carry. Meta publishes none;
#: this bounds an authored document (SECURITY-BASELINE §7) and a template
#: needing more than this is unreadable anyway.
MAX_SLOTS = 20

#: The Meta review outcomes that make a template usable, unusable, or still
#: undecided. Anything unlisted leaves the row where it is and is logged: an
#: unknown status is Meta adding a state, and guessing which side it falls on is
#: how a template becomes sendable that should not be.
_APPROVED_STATUSES = frozenset({"APPROVED"})
_PENDING_STATUSES = frozenset({"PENDING", "IN_APPEAL", "PENDING_DELETION"})
#: Not all of these are "rejected" in Meta's sense — ``PAUSED`` is a quality
#: suspension and ``DELETED`` is gone rather than refused. They are grouped
#: because the operator's next step is identical for all of them (fix it and
#: submit again), because none of them may be sent, and because failing closed
#: is the only safe direction for a gate a compliance rule depends on. The
#: reason column carries Meta's own word, so the page never claims a paused
#: template was rejected.
_REFUSED_STATUSES = frozenset({"REJECTED", "DISABLED", "PAUSED", "DELETED", "LIMIT_EXCEEDED"})


# ---------------------------------------------------------------------------
# Reading a template
# ---------------------------------------------------------------------------


def _component_texts(body_structure: Any) -> list[tuple[str, str]]:
    """``(slot prefix, text)`` for every part of a template that can hold a slot.

    One traversal, used by :func:`slots_for`, :func:`preview` and the submission
    builder, so the three cannot disagree about where a variable may appear.
    The prefixes are the platform-neutral slot vocabulary
    :class:`~apps.channels.events.OutboundMessage` documents.
    """
    if not isinstance(body_structure, dict):
        return []

    found: list[tuple[str, str]] = []
    header = body_structure.get("header")
    if isinstance(header, dict) and header.get("format", "text") == "text":
        found.append(("header", str(header.get("text") or "")))

    body = body_structure.get("body")
    if isinstance(body, dict):
        found.append(("body", str(body.get("text") or "")))

    buttons = body_structure.get("buttons")
    if isinstance(buttons, list):
        for index, button in enumerate(buttons):
            if isinstance(button, dict) and button.get("type") == "url":
                found.append((f"button.{index}", str(button.get("url") or "")))
    return found


def slots_for(template: WhatsAppTemplate) -> tuple[str, ...]:
    """Every variable this template needs filled, as ordered slot names.

    ``("header.1", "body.1", "body.2", "button.0.1")`` — the vocabulary
    ``OutboundMessage.template_variables`` carries and
    ``providers.whatsapp._template_components`` turns back into Meta's
    ``components``. Numbering restarts per component because that is how Meta
    numbers them: ``{{1}}`` in a header and ``{{1}}`` in a body are two
    different parameters.

    Deduplicated and sorted numerically, so a body that uses ``{{2}}`` twice
    asks for one value and a body that uses ``{{10}}`` does not sort before
    ``{{2}}``.
    """
    slots: list[str] = []
    for prefix, text in _component_texts(template.body_structure):
        numbers = sorted({int(match) for match in _SLOT_RE.findall(text)})
        slots.extend(f"{prefix}.{number}" for number in numbers)
    return tuple(slots[:MAX_SLOTS])


def variable_schema(template: WhatsAppTemplate) -> dict[str, Any]:
    """What a composer needs to render a form for this template.

    The shape L6-B's broadcast composer and the flow builder's template picker
    both read, so neither has to know how a ``body_structure`` is laid out.
    """
    return {
        "id": str(template.pk),
        "reference": template.reference,
        "name": template.name,
        "language": template.language,
        "category": template.category,
        "status": template.status,
        "slots": list(slots_for(template)),
        "body": _body_text(template),
    }


def _body_text(template: WhatsAppTemplate) -> str:
    body = template.body_structure.get("body") if isinstance(template.body_structure, dict) else None
    return str(body.get("text") or "") if isinstance(body, dict) else ""


def preview(template: WhatsAppTemplate, values: dict[str, str]) -> dict[str, str]:
    """The template as a contact would see it, with ``values`` filled in.

    ``values`` is keyed by slot name (``"body.1"``). Rendering goes through the
    one shared renderer — see the module docstring — which means an operator who
    types ``{{first_name}}`` as a sample value gets that text back literally
    rather than having it resolved against anything, and a value containing
    ``{% … %}`` is equally inert.

    Unfilled slots render as empty rather than being left as ``{{1}}``. That is
    the shared renderer's own rule and it is the right one here too: showing the
    operator the gap is how they notice they have not supplied the value.
    """
    rendered: dict[str, str] = {}
    for prefix, text in _component_texts(template.body_structure):
        context = RenderContext(
            variables={
                # The renderer resolves a bare token, so "body.1" becomes "1"
                # — the number Meta actually wrote in the text.
                slot.rsplit(".", 1)[-1]: value
                for slot, value in values.items()
                if slot.startswith(f"{prefix}.")
            }
        )
        rendered[prefix] = render(text, context)

    footer = template.body_structure.get("footer") if isinstance(template.body_structure, dict) else None
    if isinstance(footer, dict) and footer.get("text"):
        # No slots are allowed in a footer, so it is passed through untouched.
        rendered["footer"] = str(footer.get("text"))
    return rendered


# ---------------------------------------------------------------------------
# Submitting a template
# ---------------------------------------------------------------------------


def submission_components(template: WhatsAppTemplate) -> list[dict[str, Any]]:
    """The ``components`` Meta's create endpoint expects.

    Pure, so the submission payload is snapshot-testable without a socket.

    **Examples are generated, not asked for.** Meta refuses a template whose
    body carries variables and no ``example``, and an operator has no way to
    know that from the form. The samples are obviously placeholder text so that
    a reviewer reading them is not misled about what the template says.
    """
    structure = template.body_structure if isinstance(template.body_structure, dict) else {}
    components: list[dict[str, Any]] = []

    header = structure.get("header")
    if isinstance(header, dict) and header.get("text"):
        text = str(header["text"])[:MAX_HEADER_CHARS]
        component: dict[str, Any] = {"type": "HEADER", "format": "TEXT", "text": text}
        samples = _samples(text)
        if samples:
            component["example"] = {"header_text": samples}
        components.append(component)

    body = structure.get("body")
    body_text = str(body.get("text") or "")[:MAX_BODY_CHARS] if isinstance(body, dict) else ""
    if body_text:
        component = {"type": "BODY", "text": body_text}
        samples = _samples(body_text)
        if samples:
            # Meta nests body examples one level deeper than header ones: a list
            # of variable-sets, of which we send exactly one.
            component["example"] = {"body_text": [samples]}
        components.append(component)

    footer = structure.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        components.append({"type": "FOOTER", "text": str(footer["text"])[:MAX_FOOTER_CHARS]})

    buttons = _submission_buttons(structure.get("buttons"))
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})
    return components


def _submission_buttons(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    buttons: list[dict[str, Any]] = []
    for button in raw:
        if not isinstance(button, dict):
            continue
        text = str(button.get("text") or "")[:MAX_BUTTON_TEXT_CHARS]
        if not text:
            continue
        if button.get("type") == "url":
            url = str(button.get("url") or "")
            if not url:
                continue
            entry: dict[str, Any] = {"type": "URL", "text": text, "url": url}
            samples = _samples(url)
            if samples:
                entry["example"] = [_SLOT_RE.sub(lambda _: "example", url)]
            buttons.append(entry)
        else:
            buttons.append({"type": "QUICK_REPLY", "text": text})
    return buttons


def _samples(text: str) -> list[str]:
    """One placeholder sample per distinct slot in ``text``, in slot order."""
    numbers = sorted({int(match) for match in _SLOT_RE.findall(text)})
    return [f"sample {number}" for number in numbers]


def submit(template: WhatsAppTemplate) -> WhatsAppTemplate:
    """Send ``template`` to Meta for review and move it to ``pending``.

    Raises :class:`~apps.channels.providers.exceptions.APIError` when Meta
    refuses it — the caller is a view with an operator waiting, and "Meta said
    no" is something they have to see rather than something to swallow.

    The row is only advanced **after** Meta accepts it, so a failed submission
    leaves a draft the operator can fix rather than a template stuck in a review
    that never started.
    """
    if not _NAME_RE.match(template.name):
        raise APIError("A template name may only contain lowercase letters, digits and underscores.")

    connection = template.channel_connection
    credentials = whatsapp.credentials_of(connection)
    waba_id = credentials.get(whatsapp.WABA_ID_KEY, "")
    if not waba_id:
        raise APIError("This WhatsApp connection has no WhatsApp Business Account id stored.")

    body = whatsapp.call(
        credentials.get(whatsapp.ACCESS_TOKEN_KEY, ""),
        f"{waba_id}/message_templates",
        {
            "name": template.name,
            "language": template.language,
            "category": template.category.upper(),
            "components": submission_components(template),
        },
        timeout=BACKGROUND_TIMEOUT,
    )

    meta_id = body.get("id")
    template.meta_template_id = str(meta_id)[:64] if isinstance(meta_id, (str, int)) else ""
    template.status = WhatsAppTemplateStatus.PENDING
    template.rejected_reason = ""
    template.save(update_fields=["meta_template_id", "status", "rejected_reason", "updated_at"])
    logger.info("WhatsApp template %s submitted for review.", template.pk)
    return template


def delete_template(template: WhatsAppTemplate) -> None:
    """Delete a template at Meta, then locally.

    Meta first, and best-effort: a row removed here while Meta still holds the
    template would leave a name that can never be reused, because a second
    submission under the same name collides with the copy nobody can see. A
    failure is logged and the local row still goes — an operator who cannot
    delete a template from the product has no other way to fix it.
    """
    credentials = whatsapp.credentials_of(template.channel_connection)
    waba_id = credentials.get(whatsapp.WABA_ID_KEY, "")
    if waba_id and template.meta_template_id:
        params = {"name": template.name, "hsm_id": template.meta_template_id}
        try:
            whatsapp.call(
                credentials.get(whatsapp.ACCESS_TOKEN_KEY, ""),
                f"{waba_id}/message_templates",
                method="DELETE",
                params=params,
                timeout=BACKGROUND_TIMEOUT,
            )
        except APIError:
            logger.warning("WhatsApp template %s could not be deleted at Meta; removing it locally.", template.pk)
    template.delete()


# ---------------------------------------------------------------------------
# Polling review outcomes (SPEC §15's hourly housekeeping job)
# ---------------------------------------------------------------------------


def refresh_status(template: WhatsAppTemplate) -> bool:
    """Ask Meta where this template's review got to. True when it moved.

    One call per pending template rather than one listing per WABA: pending
    templates are few, a listing is paginated, and a template fetched by its own
    id cannot be confused with a same-named one in another language.
    """
    if not template.meta_template_id:
        return False
    credentials = whatsapp.credentials_of(template.channel_connection)
    body = whatsapp.call(
        credentials.get(whatsapp.ACCESS_TOKEN_KEY, ""),
        template.meta_template_id,
        method="GET",
        params={"fields": "id,name,language,status,category,rejected_reason"},
        timeout=BACKGROUND_TIMEOUT,
    )

    status = str(body.get("status") or "").upper()
    if status in _PENDING_STATUSES:
        return False
    if status in _APPROVED_STATUSES:
        new_status, reason = WhatsAppTemplateStatus.APPROVED, ""
    elif status in _REFUSED_STATUSES:
        raw_reason = body.get("rejected_reason")
        reason = str(raw_reason) if isinstance(raw_reason, str) and raw_reason.upper() != "NONE" else status
        new_status = WhatsAppTemplateStatus.REJECTED
    else:
        logger.warning("WhatsApp template %s came back with an unrecognised status; leaving it pending.", template.pk)
        return False

    template.status = new_status
    template.rejected_reason = reason[:500]
    template.save(update_fields=["status", "rejected_reason", "updated_at"])
    _announce(template)
    return True


def poll_pending() -> str | None:
    """The hourly job: move every pending template to its review outcome.

    Registered with L2-C's sweep through
    ``apps.queueing.housekeeping.OPTIONAL_JOB_PATHS``, which reserved the name
    ``poll_whatsapp_templates`` and the path
    ``apps.channels.providers.whatsapp.poll_template_statuses`` for this issue.

    Idempotent, as every housekeeping job must be: the sweep re-runs the whole
    list when any job fails, and a template already moved is simply no longer
    pending. One template's failure must not stop the rest — Meta rejecting a
    single fetch, or one workspace's token having been revoked, is not a reason
    to stop polling every other workspace.

    Cross-tenant on purpose: housekeeping sweeps the deployment, and there is no
    session workspace to scope by.
    """
    pending = list(
        # Cross-tenant by necessity: an hourly sweep has no workspace.
        WhatsAppTemplate.objects.unscoped()
        .filter(status=WhatsAppTemplateStatus.PENDING)
        .exclude(meta_template_id="")
        .select_related("channel_connection", "workspace")
    )
    if not pending:
        return None

    moved = 0
    failed = 0
    for template in pending:
        try:
            moved += 1 if refresh_status(template) else 0
        except APIError:
            failed += 1
            logger.warning("WhatsApp template %s could not be polled; it will be retried next hour.", template.pk)
        except Exception:
            failed += 1
            logger.exception("WhatsApp template %s failed to poll.", template.pk)

    if not moved and not failed:
        return None
    return f"polled {len(pending)} WhatsApp template(s): {moved} reviewed, {failed} unreachable"


def _announce(template: WhatsAppTemplate) -> None:
    """Tell the workspace's admins what Meta decided.

    The event type ``whatsapp_template_reviewed`` was registered by L2-E (#7),
    naming this issue as its consumer, so the copy already exists and this
    supplies only the context. A failure here must not undo the status change —
    the row is the truth and the notification is the courtesy.
    """
    from apps.notifications.engine import notify

    try:
        notify(
            template.workspace,
            "whatsapp_template_reviewed",
            context={
                "template_name": template.name,
                "status": template.get_status_display().lower(),
                "reason": template.rejected_reason,
                "action_url": reverse(
                    "channels:whatsapp_templates",
                    kwargs={"workspace_id": template.workspace_id},
                ),
            },
        )
    except Exception:
        logger.exception("Could not notify a template review outcome for %s.", template.pk)


# ---------------------------------------------------------------------------
# Selectors
# ---------------------------------------------------------------------------


def approved_templates_for(workspace: Any, *, connection: Any = None) -> list[WhatsAppTemplate]:
    """Templates a send may reference right now.

    The selector L6-B's broadcast composer and the flow builder's template
    picker both read, so neither reimplements "approved" — and so a template
    that becomes unusable stops being offered everywhere at once.
    """
    queryset = WhatsAppTemplate.objects.for_workspace(workspace).filter(status=WhatsAppTemplateStatus.APPROVED)
    if connection is not None:
        queryset = queryset.filter(channel_connection=connection)
    return list(queryset.select_related("channel_connection"))


def whatsapp_connections_for(workspace: Any) -> list[ChannelConnection]:
    """The workspace's live WhatsApp connections, newest last."""
    return list(
        ChannelConnection.objects.for_workspace(workspace)
        .filter(platform=Platform.WHATSAPP.value)
        .exclude(status=ConnectionStatus.DISABLED)
        .order_by("created_at")
    )


def cost_hint_for(workspace: Any) -> WhatsAppCostHint:
    """This workspace's per-category price estimates, saved or default.

    Returns an **unsaved** instance when the workspace has entered nothing, so a
    read never writes: a settings page that created a row on first view would
    put an INSERT on a GET.
    """
    existing = WhatsAppCostHint.objects.for_workspace(workspace).first()
    if existing is not None:
        return existing
    return WhatsAppCostHint(workspace=workspace)


def save_cost_hint(workspace: Any, *, currency: str, amounts: dict[str, Decimal]) -> WhatsAppCostHint:
    """Store the per-category estimates, replacing whatever was there."""
    with transaction.atomic():
        hint = WhatsAppCostHint.objects.for_workspace(workspace).first()
        if hint is None:
            hint = WhatsAppCostHint(workspace=workspace)
        hint.currency = currency
        for category, amount in amounts.items():
            setattr(hint, category, amount)
        hint.save()
    return hint
