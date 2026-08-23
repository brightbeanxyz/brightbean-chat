"""WhatsApp's connect flow and its template manager (issue #19).

Separate from :mod:`apps.channels.views` because that module ships what every
platform shares — the connection row, its status, its webhook URL and secret —
and separate from :mod:`apps.channels.providers.whatsapp` because the adapter is
the thing a later platform copies and a view is not part of it. The adapter owns
the Cloud API; this module owns what an operator sees. ``views_telegram``
established the split and the reasoning is identical.

Everything here is gated on ``manage_channels``, which
``apps.members.roles._ADMIN_ONLY_KEYS`` makes admin-only: these pages hold a
credential that can read and send as the business, and they submit material to
Meta under the workspace's own account.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.channels import whatsapp_templates
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.forms_whatsapp import WhatsAppConnectForm, WhatsAppCostHintForm, WhatsAppTemplateForm
from apps.channels.models import ChannelConnection, WhatsAppTemplate, WhatsAppTemplateStatus
from apps.channels.providers import whatsapp
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = [
    "whatsapp_connect",
    "whatsapp_cost_hints",
    "whatsapp_template_delete",
    "whatsapp_template_edit",
    "whatsapp_template_new",
    "whatsapp_template_preview",
    "whatsapp_template_submit",
    "whatsapp_templates_list",
]

#: Shown when Meta will not accept the pasted credentials. Deliberately one
#: message for every reason the verification call can fail — wrong token,
#: expired token, wrong phone number id, Meta unreachable, missing permission.
#: An operator's next step is the same in all of them (check it in Business
#: Manager and try again), and a message that distinguished them would be an
#: oracle for whether a given id or token is real.
REJECTED_MESSAGE = (
    "Meta did not accept those details. Check the phone number ID and that the system user token "
    "has whatsapp_business_messaging and whatsapp_business_management, then try again."
)

#: Shown when the credentials work but the webhook subscription does not. A
#: separate message because the fix is genuinely different: this one is an app
#: that has not been given the WABA, rather than a bad token.
SUBSCRIBE_FAILED = (
    "Those credentials work, but this app could not be subscribed to the WhatsApp Business Account's "
    "webhooks. Nothing will be delivered until it is; see docs/channels/whatsapp.md."
)


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_channels")
def whatsapp_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Connect a WhatsApp number from Cloud API credentials (SPEC §6.5).

    The order is load-bearing, and it is Telegram's. The verification call comes
    **first**, before anything is written, because it is the only thing that can
    tell us the number's display form and prove the token — and because a
    credential that does not work should leave no trace. The webhook
    subscription comes last: if Meta will not subscribe the app, the connection
    is removed rather than left in the list looking connected while nothing is
    ever delivered to it.

    The token is never rendered back, not even into the form on a failed submit
    (CONTRIBUTING: "never render a stored secret", and this one is not even
    stored yet) — the password widget declines to re-render its value.
    """
    form = WhatsAppConnectForm(request.POST or None)
    error = ""
    if request.method == "POST" and form.is_valid():
        error = _connect(request, form.cleaned_data)
        if not error:
            return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/whatsapp_connect.html",
        {
            "form": form,
            "error": error,
            "webhook_url": request.build_absolute_uri(
                reverse("webhook_platform", kwargs={"platform": Platform.WHATSAPP.value})
            ),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _connect(request: WorkspaceRequest, data: dict[str, Any]) -> str:
    """Do the connect. Returns "" on success or the message to show."""
    token = data["access_token"]
    try:
        number = whatsapp.verify_phone_number(token, data["phone_number_id"])
    except APIError:
        # No exception detail in the message and none in the log: the
        # surrounding code path is the one place this token exists in plain
        # text (SECURITY-BASELINE §5).
        logger.info("WhatsApp connect: verification was rejected for workspace %s.", request.workspace.pk)
        return REJECTED_MESSAGE

    display = str(number.get("display_phone_number") or "") or str(number.get("verified_name") or "")
    connection = ChannelConnection(
        workspace=request.workspace,
        platform=Platform.WHATSAPP.value,
        display_name=(display or data["phone_number_id"])[:200],
        external_id=data["phone_number_id"],
    )
    whatsapp.store_credentials(
        connection,
        token=token,
        waba_id=data["waba_id"],
        phone_number_id=data["phone_number_id"],
    )
    # A secret is minted even though Meta signs with the app secret rather than
    # this one: the connection detail page offers rotation for every platform,
    # and a row with an empty digest would collide with every other empty one
    # under the partial unique constraint.
    connection.rotate_webhook_secret()

    try:
        # The savepoint is not optional and is not about the network call: an
        # IntegrityError marks the surrounding transaction unusable, so without
        # atomic() a duplicate number would poison every query for the rest of
        # the request rather than becoming the form error below.
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # SPEC §5's unique (platform, external_id) is deployment-wide, so this
        # can be another workspace's row. The wording never says which
        # (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    # Outside the savepoint, deliberately, for the reason
    # ``views_telegram._connect`` sets out: holding a transaction open across a
    # 30-second network round trip pins a database connection, and a handful of
    # operators connecting numbers while Meta is degraded would exhaust the
    # pool. The cost is that the failure path cleans up rather than rolls back.
    try:
        whatsapp.subscribe_app(token, data["waba_id"])
    except APIError:
        logger.info("WhatsApp connect: subscribed_apps failed for workspace %s.", request.workspace.pk)
        connection.delete()
        return SUBSCRIBE_FAILED

    messages.success(request, f"Connected {connection.display_name}. Send it a message to check it works.")
    return ""


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_channels")
def whatsapp_templates_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Every template in this workspace, with its review state (SPEC §6.5)."""
    templates = list(
        WhatsAppTemplate.objects.for_workspace(request.workspace)
        .select_related("channel_connection")
        .order_by("name", "language")
    )
    cost_hint = whatsapp_templates.cost_hint_for(request.workspace)
    return render(
        request,
        "channels/whatsapp_templates.html",
        {
            "templates": [
                {
                    "template": item,
                    "slots": whatsapp_templates.slots_for(item),
                    # Resolved here rather than by a template filter: the cost
                    # is per category and the template language cannot index a
                    # mapping by a row's own value.
                    "cost": cost_hint.amount_for(item.category),
                }
                for item in templates
            ],
            "connections": whatsapp_templates.whatsapp_connections_for(request.workspace),
            "cost_hint": cost_hint,
            "new_url": reverse("channels:whatsapp_template_new", kwargs={"workspace_id": workspace_id}),
            "cost_url": reverse("channels:whatsapp_cost_hints", kwargs={"workspace_id": workspace_id}),
        },
    )


@login_required
@require_permission("manage_channels")
def whatsapp_template_new(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Author a new template. Saved as a draft; submission is a second step."""
    return _edit(request, workspace_id, template=None)


@login_required
@require_permission("manage_channels")
def whatsapp_template_edit(request: WorkspaceRequest, workspace_id: str, template_id: str) -> HttpResponse:
    """Edit a template that has not been submitted yet.

    An approved or pending template is **not** editable, and that is Meta's rule
    rather than ours: the copy under review is the one Meta holds, and editing
    the local row would make the two disagree with nothing to say which is live.
    The page offers a copy instead.
    """
    template = get_scoped_object_or_404(WhatsAppTemplate, request.workspace, pk=template_id)
    return _edit(request, workspace_id, template=template)


def _edit(request: WorkspaceRequest, workspace_id: str, *, template: WhatsAppTemplate | None) -> HttpResponse:
    editable = template is None or template.status in {
        WhatsAppTemplateStatus.DRAFT,
        WhatsAppTemplateStatus.REJECTED,
    }
    form = WhatsAppTemplateForm(
        request.POST or None,
        instance=template,
        workspace=request.workspace,
    )

    if request.method == "POST" and editable and form.is_valid():
        saved = form.save(commit=False)
        saved.workspace = request.workspace
        saved.body_structure = form.body_structure()
        # The transition belongs to the service, beside submit and delete — see
        # its docstring for why clearing the Meta id is part of it.
        whatsapp_templates.reset_to_draft(saved)
        try:
            with transaction.atomic():
                saved.save()
        except IntegrityError:
            form.add_error("name", "This number already has a template with that name and language.")
        else:
            messages.success(request, f"Saved {saved.name}. Submit it when you are ready for Meta to review it.")
            return redirect(reverse("channels:whatsapp_templates", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/whatsapp_template_form.html",
        {
            "form": form,
            "template": template,
            "editable": editable,
            "preview_url": reverse("channels:whatsapp_template_preview", kwargs={"workspace_id": workspace_id}),
            "list_url": reverse("channels:whatsapp_templates", kwargs={"workspace_id": workspace_id}),
            # Django's template language has no escape for a literal "{{", and
            # {% templatetag %} spells it in two pieces; these are the readable
            # way to show an operator what to type.
            "open_brace": "{{",
            "close_brace": "}}",
        },
    )


@login_required
@require_permission("manage_channels")
@require_POST
def whatsapp_template_preview(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Render the template being typed, with sample values (HTMX fragment).

    Takes the posted text rather than a saved row, so the preview updates while
    an operator types and works before anything has been saved. It writes
    nothing and reads nothing belonging to a tenant, which is why it needs no
    object lookup — but it is still ``manage_channels`` and still POST-only,
    because it echoes submitted content back into the page and a GET version
    would be linkable.

    The rendering path is
    :func:`apps.channels.whatsapp_templates.preview`, which is the same shared,
    engine-free substitution the send path uses (SECURITY-BASELINE §3). Nothing
    here is marked safe; the template escapes it.
    """
    unsaved = WhatsAppTemplate(
        workspace=request.workspace,
        body_structure={
            "header": {"format": "text", "text": _posted(request, "header_text", 200)},
            "body": {"text": _posted(request, "body_text", 2000)},
            "footer": {"text": _posted(request, "footer_text", 200)},
        },
    )
    slots = whatsapp_templates.slots_for(unsaved)
    values = {slot: _posted(request, f"sample.{slot}", 200) or f"sample {slot.rsplit('.', 1)[-1]}" for slot in slots}
    return render(
        request,
        "channels/partials/_whatsapp_template_preview.html",
        {
            "rendered": whatsapp_templates.preview(unsaved, values),
            # Pairs rather than a dict: a Django template cannot index a dict by
            # a variable key, and adding a filter for it would be a new piece of
            # shared machinery for one page.
            "samples": [{"slot": slot, "value": values[slot]} for slot in slots],
        },
    )


def _posted(request: WorkspaceRequest, key: str, limit: int) -> str:
    """One bounded POST value. Everything the preview reads goes through this."""
    value = request.POST.get(key) or ""
    return value[:limit] if isinstance(value, str) else ""


@login_required
@require_permission("manage_channels")
@require_POST
def whatsapp_template_submit(request: WorkspaceRequest, workspace_id: str, template_id: str) -> HttpResponse:
    """Send a draft to Meta for review (SPEC §6.5).

    The outcome arrives hours later through the hourly poll, not here, so this
    view's job ends at "Meta accepted the submission".
    """
    template = get_scoped_object_or_404(WhatsAppTemplate, request.workspace, pk=template_id)
    try:
        whatsapp_templates.submit(template)
    except APIError as exc:
        # str(exc) is the adapter's own sentence — host and status code, never
        # the URL or the response body (providers/exceptions.py).
        messages.error(request, f"Meta would not accept {template.name}: {exc}")
    else:
        messages.success(request, f"{template.name} is with Meta for review. This usually takes minutes to a day.")
    return redirect(reverse("channels:whatsapp_templates", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_channels")
@require_POST
def whatsapp_template_delete(request: WorkspaceRequest, workspace_id: str, template_id: str) -> HttpResponse:
    """Delete a template here and at Meta."""
    template = get_scoped_object_or_404(WhatsAppTemplate, request.workspace, pk=template_id)
    name = template.name
    whatsapp_templates.delete_template(template)
    messages.success(request, f"Deleted {name}.")
    return redirect(reverse("channels:whatsapp_templates", kwargs={"workspace_id": workspace_id}))


# ---------------------------------------------------------------------------
# Cost hints
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_channels")
def whatsapp_cost_hints(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Per-category price estimates, shown beside a template (SPEC §6.5, §22).

    "WhatsApp costs are the self-hoster's Meta bill; OpenChat only warns, never
    meters." These numbers are entered by hand because Meta prices per country,
    per category and per agreement — a number this product fetched would be
    wrong in a way that looked authoritative.
    """
    hint = whatsapp_templates.cost_hint_for(request.workspace)
    form = WhatsAppCostHintForm(request.POST or None, instance=hint)
    if request.method == "POST" and form.is_valid():
        whatsapp_templates.save_cost_hint(
            request.workspace,
            currency=form.cleaned_data["currency"],
            # Already Decimal: DecimalField.clean returns one, and the form's
            # own clean_* methods substitute Decimal("0") for a blank.
            amounts={category: form.cleaned_data[category] for category in ("marketing", "utility", "authentication")},
        )
        messages.success(request, "Saved the cost estimates.")
        return redirect(reverse("channels:whatsapp_cost_hints", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/whatsapp_cost_hints.html",
        {
            "form": form,
            "list_url": reverse("channels:whatsapp_templates", kwargs={"workspace_id": workspace_id}),
        },
    )
