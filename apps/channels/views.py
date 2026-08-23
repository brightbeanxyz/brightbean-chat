"""Workspace settings → Channels (SPEC §5, issue #4).

Admin-only: ``manage_channels`` is one of ``_ADMIN_ONLY_KEYS`` in
``apps.members.roles``, and this page mints the secret a platform authenticates
with.

**Minimal by design.** The issue says so — "per-platform placeholder connect
panels (real flows arrive with each adapter)" — and the reason is that each
platform's connect flow is genuinely different: a BotFather token pasted into a
field, a Meta OAuth round trip, Twilio credentials plus a number. Building one
form that pretends they are the same shape means building it wrong six times.
What ships here is the frame every adapter will hang its flow on: the row, its
status, its webhook URL and its secret.

**The webhook secret is shown exactly once.** It is rendered in the response to
the POST that created or rotated it, and never again — no redirect, no
``messages`` framework. That is not fussiness: ``messages`` is stored in the
session, which for this project is a database table, so a redirect-then-show
would leave a live platform credential sitting in ``django_session`` in plain
text for the life of the session (SECURITY-BASELINE §5). Post/Redirect/Get is
worth giving up for that; the double-submit it protects against would just mint
another secret.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.channels.capabilities import capabilities_for
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR, ChannelConnectionForm
from apps.channels.models import ChannelConnection, ConnectionStatus, WebhookEventLog
from apps.channels.policy import policy_for
from apps.channels.providers import email_backends
from apps.channels.providers.base import Adapter
from apps.channels.providers.exceptions import AdapterError
from apps.channels.registry import AdapterNotRegisteredError, adapter_for, connect_route_for, has_adapter
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

PLATFORM_LABELS = dict(Platform.choices)

#: Which issue delivers each platform's real connect flow. Shown on the
#: placeholder panels so an operator looking at an empty page knows whether they
#: have misconfigured something or are simply early.
CONNECT_FLOW_ISSUES: dict[str, str] = {
    # Empty, as of #21: every platform in ``Platform`` now has a guided connect
    # flow, so the list template links to those rather than naming an issue.
    # Kept rather than deleted because Layer 7 adds platforms, and the next one
    # to arrive ahead of its connect view needs somewhere to say so. A platform
    # leaves this table on the day its connect view lands, which
    # ``test_views.py`` asserts rather than trusting.
}

#: One sentence per guided connect flow, because the flows are not alike: a
#: BotFather token is one field and Meta's Cloud API is three plus a
#: subscription. The list page used to carry Telegram's wording inline, which
#: silently became wrong for the second platform that got a flow.
#:
#: **Every entry in ``CONNECT_ROUTES`` needs one**, and that pairing is asserted
#: by ``test_views.py`` rather than left to whoever adds the next adapter. It
#: has been missed twice already — Instagram's flow and SMS's each landed on
#: main while this dict was being edited on another branch, and each merge
#: produced a row reading "set it up — " with nothing after the dash. Neither
#: branch could see it alone, which is exactly what a test is for.
CONNECT_HINTS: dict[str, str] = {
    Platform.TELEGRAM: "paste a BotFather token and we do the rest.",
    Platform.INSTAGRAM: "sign in with the Instagram account and grant the messaging permissions.",
    Platform.WHATSAPP: "paste your Cloud API ids and system user token; we verify them with Meta first.",
    Platform.MESSENGER: "sign in with Facebook and pick the page to connect.",
    Platform.SMS: "paste your Twilio account SID, auth token and number.",
    Platform.EMAIL: "pick SMTP, Resend or SES; we check the credentials before saving them.",
}

#: Extra settings pages a platform brings with it, as ``(label, route)`` pairs.
#: A dict rather than a per-platform ``if`` in the template, for the same reason
#: ``CONNECT_ROUTES`` is one: the next adapter adds a line here instead of
#: teaching the list page about itself.
PLATFORM_EXTRA_LINKS: dict[str, tuple[tuple[str, str], ...]] = {
    Platform.WHATSAPP: (
        ("Message templates", "channels:whatsapp_templates"),
        ("Cost estimates", "channels:whatsapp_cost_hints"),
    ),
}

#: Statuses an operator may set by hand. ``needs_reauth`` is absent because an
#: adapter sets it from a platform's own rejection — letting a human declare it
#: would mean a connection stuck in a state nothing clears.
SETTABLE_STATUSES = frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.DISABLED})

#: The email webhook's provider segment when the connection does not name one.
#: SMTP is the only transport that needs no provider-specific parsing.
DEFAULT_EMAIL_PROVIDER = "smtp"

#: Sentinel for "the caller has not looked this up". Distinct from None, which
#: is the real answer "this connection has never received anything".
_UNFETCHED = object()


def _webhook_url(request: WorkspaceRequest, connection: ChannelConnection) -> str:
    """The URL to paste into the platform's console (SPEC §7.1).

    Per-connection for SMS and email, one shared URL per platform for the rest.
    Absolute, because that is the form the operator has to type somewhere else.

    **SMS goes through the adapter's own builder**, which reads ``APP_URL``
    rather than this request. Twilio's signature is an HMAC over the URL it was
    configured with, so ``verify_webhook`` recomputes that string — and a page
    that showed ``request.build_absolute_uri`` while the adapter verified
    ``APP_URL`` would, behind any reverse proxy, hand the operator a URL whose
    every delivery is then rejected with nothing to say why.
    """
    if connection.platform == Platform.SMS:
        from apps.channels.providers import sms

        return sms.webhook_url(connection)
    if connection.platform == Platform.EMAIL:
        path = reverse(
            "webhook_email",
            kwargs={"provider": _email_provider(connection), "connection_id": connection.pk},
        )
    else:
        path = reverse("webhook_platform", kwargs={"platform": connection.platform})
    return request.build_absolute_uri(path)


def _email_provider(connection: ChannelConnection) -> str:
    """Which provider segment this email connection's webhook URL should carry.

    The segment tells the adapter which body shape to expect (SPEC §6.7), and
    this URL is what the operator pastes into their provider's console — so
    hardcoding one value meant a Resend or SES deployment configured a URL that
    routed correctly and then handed the adapter the wrong shape hint, which
    would read as a provider bug.

    Delegated to the adapter's own reader now that #21 has shipped one, so the
    URL this page prints and the verifier the webhook actually runs cannot
    disagree — they read the same key through the same function, and it answers
    with one of three literals from that module whatever the column holds.
    """
    return email_backends.provider_for(connection)


def _connection_context(
    request: WorkspaceRequest,
    connection: ChannelConnection,
    *,
    last_event_at: Any = _UNFETCHED,
) -> dict[str, Any]:
    """Everything one connection's row or detail panel needs.

    Capabilities and policy come from the static tables rather than the adapter,
    so the page is complete for a platform whose adapter has not shipped —
    which, in this layer, is all of them (ROADMAP contract 4).
    """
    return {
        "connection": connection,
        "label": PLATFORM_LABELS.get(connection.platform, connection.platform),
        # The per-platform settings page, where the platform has one. SMS is the
        # first: SPEC §6.6's mandated replies and the A2P checklist belong to the
        # workspace rather than to one number, so they are not fields on this
        # row — but this page is where an operator looking at that number goes.
        "settings_url": _settings_url(connection, request.workspace.pk),
        "webhook_url": _webhook_url(request, connection),
        "capabilities": capabilities_for(connection.platform),
        "policy": policy_for(connection.platform),
        "adapter_ready": has_adapter(connection.platform),
        "connect_flow_issue": CONNECT_FLOW_ISSUES.get(connection.platform, ""),
        # Passed in by the list view, which reads every row's in one grouped
        # query; fetched here for the single-connection pages, where one query
        # is the whole cost anyway.
        "last_event_at": _last_event_at(connection) if last_event_at is _UNFETCHED else last_event_at,
    }


#: Platforms with a workspace-level settings page beyond the connection row.
SETTINGS_ROUTES: dict[str, str] = {Platform.SMS.value: "channels:sms_settings"}


def _settings_url(connection: ChannelConnection, workspace_id: Any) -> str:
    """The platform's own settings page, or "" where it has none."""
    route = SETTINGS_ROUTES.get(connection.platform, "")
    return reverse(route, kwargs={"workspace_id": workspace_id}) if route else ""


def _connect_url(platform: str, workspace_id: str) -> str:
    """The guided connect route for ``platform``, or "" if it has none yet."""
    route = connect_route_for(platform)
    return reverse(route, kwargs={"workspace_id": workspace_id}) if route else ""


def _last_events_for(connections: list[ChannelConnection]) -> dict[Any, Any]:
    """Newest ``received_at`` per connection, in one query.

    The list page renders every connection a workspace has, and asking per row
    made it an N+1 against a table the webhook endpoint writes to constantly.
    One grouped aggregate answers the whole page.
    """
    if not connections:
        return {}
    rows = (
        WebhookEventLog.objects.filter(connection__in=connections)
        .values("connection_id")
        .annotate(latest=Max("received_at"))
    )
    return {row["connection_id"]: row["latest"] for row in rows}


def _last_event_at(connection: ChannelConnection) -> Any:
    """When this connection last received anything, or None.

    Webhook health, as the settings card shows it (issue #12). "Nothing has
    arrived yet" and "nothing has arrived since Tuesday" are the two ways a
    misconfigured webhook presents, and neither is visible from the connection
    row itself — a bot with a wrong ``setWebhook`` URL looks exactly like a bot
    nobody has messaged.

    ``WebhookEventLog`` carries no workspace column by design (see its model
    docstring), so this filters by the connection, which is already scoped by
    the caller. ``values()`` because the row itself is a raw attacker-supplied
    payload we have no reason to load.
    """
    row = WebhookEventLog.objects.filter(connection=connection).order_by("-received_at").values("received_at").first()
    return row["received_at"] if row else None


@login_required
@require_permission("manage_channels")
@require_GET
def connection_list(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Every channel this workspace has connected."""
    connections = list(ChannelConnection.objects.for_workspace(request.workspace))
    latest = _last_events_for(connections)
    return render(
        request,
        "channels/list.html",
        {
            "rows": [
                _connection_context(request, connection, last_event_at=latest.get(connection.pk))
                for connection in connections
            ],
            "platforms": [
                {
                    "value": value,
                    "label": label,
                    "adapter_ready": has_adapter(value),
                    "issue": CONNECT_FLOW_ISSUES.get(value, ""),
                    # A guided connect flow where one exists. Telegram's is the
                    # only one today (#12); each Layer-5 adapter adds its own
                    # here rather than the template growing a per-platform if.
                    "connect_url": _connect_url(value, workspace_id),
                    "connect_hint": CONNECT_HINTS.get(value, ""),
                    "extra_links": [
                        {"label": label, "url": reverse(route, kwargs={"workspace_id": workspace_id})}
                        for label, route in PLATFORM_EXTRA_LINKS.get(value, ())
                    ],
                }
                for value, label in Platform.choices
            ],
        },
    )


@login_required
@require_permission("manage_channels")
def connection_create(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Add a connection and show its webhook secret, once."""
    if request.method == "POST":
        form = ChannelConnectionForm(request.POST)
        if form.is_valid():
            connection = form.save(commit=False)
            connection.workspace = request.workspace
            secret = connection.rotate_webhook_secret()
            try:
                with transaction.atomic():
                    connection.save()
            except IntegrityError:
                # The form's duplicate check is a read, so it is check-then-
                # insert: two workspaces submitting the same (platform,
                # external_id) can both validate before either commits, and the
                # loser used to raise straight out of the view as a 500. The
                # constraint is the real arbiter; this turns losing the race
                # into the same field error the pre-check would have produced.
                # The savepoint is what keeps the failed insert from poisoning
                # the rest of the request.
                form.add_error("external_id", DUPLICATE_ACCOUNT_ERROR)
            else:
                return _render_secret(request, connection, secret, created=True)
    else:
        form = ChannelConnectionForm()

    return render(
        request,
        "channels/new.html",
        {"form": form, "platforms": Platform.choices, "connect_flow_issues": CONNECT_FLOW_ISSUES},
    )


@login_required
@require_permission("manage_channels")
@require_GET
def connection_detail(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """One connection: status, webhook URL, what the platform can carry.

    Never renders the secret — see the module docstring.
    """
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)
    return render(request, "channels/detail.html", _connection_context(request, connection))


@login_required
@require_permission("manage_channels")
@require_POST
def connection_set_status(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Enable or disable a connection.

    A disabled connection stops ingesting: ``_connection_by_id`` in the webhook
    view excludes it, so the platform's deliveries get the same 403 as an
    unknown connection. Switching a channel off has to actually switch it off.
    """
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)
    status = request.POST.get("status", "")
    if status not in SETTABLE_STATUSES:
        messages.error(request, "That is not a status you can set by hand.")
    else:
        connection.status = status
        connection.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{connection.display_name} is now {connection.get_status_display().lower()}.")
    return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("manage_channels")
@require_POST
def connection_rotate_secret(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Mint a new webhook secret and show it once.

    Rotating invalidates the old one immediately, which means inbound deliveries
    fail with a 403 until the platform presents the new one.

    For most platforms that means an operator pasting it into a console. For the
    ones that hold the secret over their own API — Telegram sets it through
    ``setWebhook`` — there is no console, so rotating without telling the
    platform would leave a connection nothing could repair. ``_push_secret``
    does the telling, and its result is what the template reports: an operator
    has to know whether they still have work to do.
    """
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)
    secret = connection.rotate_webhook_secret()
    connection.save(update_fields=["webhook_secret", "webhook_secret_digest", "updated_at"])
    pushed = _push_secret(connection, secret)
    return _render_secret(request, connection, secret, created=False, pushed=pushed)


@login_required
@require_permission("manage_channels")
@require_POST
def connection_delete(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> HttpResponse:
    """Remove a connection and, by cascade, its webhook event log.

    The platform is told first, so a bot we are about to forget stops delivering
    to a URL that will answer 403 forever after (Telegram's ``deleteWebhook``,
    and whatever each Layer-5 platform's equivalent turns out to be). Best
    effort by contract — see ``Adapter.on_disconnect`` — because the operator
    asked to disconnect and a platform being down is not a reason to refuse.
    """
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=connection_id)
    name = connection.display_name
    _notify_disconnect(connection)
    connection.delete()
    messages.success(request, f"Disconnected {name}.")
    return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))


def _push_secret(connection: ChannelConnection, secret: str) -> bool | None:
    """Hand the new secret to the platform. True/False, or None if it has no API for it.

    None and True are different answers and the template renders them
    differently: None means "now go and paste this into the console", True means
    "already done, there is nothing left for you to do", and False means the
    channel is down until this is retried.

    The adapter's own ``on_webhook_secret_rotated`` is a no-op by default, which
    is what None reports — checked by identity against the base implementation
    rather than by a per-platform list, so a Layer-5 adapter that implements it
    starts being reported correctly with no edit here.
    """
    try:
        adapter = adapter_for(connection.platform)
    except AdapterNotRegisteredError:
        return None
    if type(adapter).on_webhook_secret_rotated is Adapter.on_webhook_secret_rotated:
        return None
    try:
        adapter.on_webhook_secret_rotated(connection, secret)
    except Exception:
        # No secret in the log: this is the one moment it is readable.
        logger.exception("Could not push a rotated webhook secret to %s", connection.platform)
        return False
    return True


def _notify_disconnect(connection: ChannelConnection) -> None:
    """Tell the platform to stop, and never let that stop the disconnect."""
    try:
        adapter_for(connection.platform).on_disconnect(connection)
    except AdapterNotRegisteredError:
        # Nothing to tell: the platform has no adapter yet, which is the state
        # five of the six are in.
        return
    except (AdapterError, OSError):
        logger.warning("Could not tell %s to stop delivering for connection %s.", connection.platform, connection.pk)
    except Exception:
        logger.exception("Unexpected failure disconnecting connection %s.", connection.pk)


def _render_secret(
    request: WorkspaceRequest,
    connection: ChannelConnection,
    secret: str,
    *,
    created: bool,
    pushed: bool | None = None,
) -> HttpResponse:
    """Render the one and only view of a webhook secret."""
    context = _connection_context(request, connection)
    context.update({"secret": secret, "created": created, "pushed": pushed})
    return render(request, "channels/secret.html", context)
