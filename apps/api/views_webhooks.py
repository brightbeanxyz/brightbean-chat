"""Outbound-webhook settings, mounted at ``/w/<uuid:workspace_id>/settings/webhooks/``.

Workspace-tier, because ``outbound_webhook`` is workspace-scoped in SPEC §5 —
its url, its secret and its subscriptions belong to one workspace's data, not to
the organization. Gated on ``manage_workspace_settings``, alongside the other
pages under ``/settings/``.

Same secret discipline as the API keys page and as ``apps/channels/views.py``:
a freshly minted endpoint secret is rendered in the response to the POST that
minted it, never through a redirect and never through ``messages`` (which is a
database-backed session in this project).
"""

from __future__ import annotations

from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.api.delivery import send_test_event
from apps.api.events import EVENT_LABELS, SUBSCRIBABLE_EVENTS
from apps.api.models import OutboundWebhook, WebhookDelivery
from apps.api.services import ApiKeysError, create_webhook, rotate_webhook_secret, update_webhook
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

__all__ = [
    "webhook_create",
    "webhook_delete",
    "webhook_detail",
    "webhook_list",
    "webhook_rotate_secret",
    "webhook_test",
    "webhook_update",
]


def _event_choices(selected: Any = ()) -> list[dict[str, Any]]:
    chosen = set(selected or ())
    return [
        {"value": event, "label": EVENT_LABELS.get(event, event), "checked": event in chosen}
        for event in SUBSCRIBABLE_EVENTS
    ]


def _webhook_or_404(request: WorkspaceRequest, webhook_id: Any) -> OutboundWebhook:
    return get_scoped_object_or_404(OutboundWebhook, request.workspace, pk=webhook_id)


def _list_url(workspace_id: Any) -> str:
    return reverse("api_webhooks:list", kwargs={"workspace_id": workspace_id})


def _detail_url(workspace_id: Any, webhook_id: Any) -> str:
    return reverse("api_webhooks:detail", kwargs={"workspace_id": workspace_id, "webhook_id": webhook_id})


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def webhook_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Every endpoint in this workspace."""
    webhooks = OutboundWebhook.objects.for_workspace(request.workspace).order_by("url")
    return render(
        request,
        "api/webhooks_list.html",
        {
            "webhooks": webhooks,
            "event_choices": _event_choices(),
            "event_labels": EVENT_LABELS,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def webhook_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Create an endpoint and show its secret once."""
    try:
        webhook = create_webhook(
            workspace=request.workspace,
            url=request.POST.get("url", ""),
            events=request.POST.getlist("events"),
        )
    except ApiKeysError as exc:
        webhooks = OutboundWebhook.objects.for_workspace(request.workspace).order_by("url")
        return render(
            request,
            "api/webhooks_list.html",
            {
                "webhooks": webhooks,
                "event_choices": _event_choices(request.POST.getlist("events")),
                "event_labels": EVENT_LABELS,
                "error": str(exc),
                "submitted_url": request.POST.get("url", ""),
            },
            status=400,
        )

    return render(
        request,
        "api/webhook_secret.html",
        {
            "webhook": webhook,
            "plaintext": webhook.secret,
            "back_url": _detail_url(workspace_id, webhook.pk),
            "heading": "Endpoint created",
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_GET
def webhook_detail(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """One endpoint, its subscriptions, and its recent deliveries.

    The delivery list is bounded by the same number housekeeping trims to, so
    the page shows everything the table keeps rather than a window into it.
    """
    from django.conf import settings

    webhook = _webhook_or_404(request, webhook_id)
    deliveries = (
        WebhookDelivery.objects.for_workspace(request.workspace)
        .filter(webhook=webhook)
        .order_by("-created_at")[: settings.API_WEBHOOK_DELIVERY_LOG_KEEP]
    )
    return render(
        request,
        "api/webhook_detail.html",
        {
            "webhook": webhook,
            "deliveries": deliveries,
            "event_choices": _event_choices(webhook.events),
            "event_labels": EVENT_LABELS,
            "failure_limit": settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES,
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def webhook_update(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Edit an endpoint's URL, subscriptions and enabled flag."""
    webhook = _webhook_or_404(request, webhook_id)
    try:
        update_webhook(
            webhook,
            url=request.POST.get("url", ""),
            events=request.POST.getlist("events"),
            enabled=request.POST.get("enabled") == "on",
        )
    except ApiKeysError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Endpoint updated.")
    return redirect(_detail_url(workspace_id, webhook.pk))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def webhook_rotate_secret(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Mint a new signing secret and show it once.

    Deliveries signed with the old secret stop verifying the moment this
    returns; the page says so, because a rotation nobody told the receiver about
    is an outage.
    """
    webhook = _webhook_or_404(request, webhook_id)
    plaintext = rotate_webhook_secret(webhook)
    return render(
        request,
        "api/webhook_secret.html",
        {
            "webhook": webhook,
            "plaintext": plaintext,
            "back_url": _detail_url(workspace_id, webhook.pk),
            "heading": "New signing secret",
        },
    )


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def webhook_test(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Deliver a synthetic event now and report what happened.

    Synchronous, and outside the failure accounting: a test against a receiver
    that is not ready yet must not push a healthy endpoint towards auto-disable.
    """
    webhook = _webhook_or_404(request, webhook_id)
    delivery = send_test_event(webhook)
    if delivery.succeeded:
        messages.success(request, f"Test delivered — the endpoint answered {delivery.response_code}.")
    else:
        messages.error(request, f"Test failed: {delivery.error or delivery.status}.")
    return redirect(_detail_url(workspace_id, webhook.pk))


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def webhook_delete(request: WorkspaceRequest, workspace_id: str, webhook_id: str) -> HttpResponse:
    """Delete an endpoint and its delivery log."""
    webhook = _webhook_or_404(request, webhook_id)
    url = webhook.url
    webhook.delete()
    messages.success(request, f"Deleted {url}.")
    return redirect(_list_url(workspace_id))
