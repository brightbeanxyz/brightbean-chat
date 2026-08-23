"""Messenger's guided connect flow (issue #18).

Separate from :mod:`apps.channels.views` for the reason issue #4 gives — the
generic frame ships the connection row, its status and its webhook URL, and each
platform's real connect flow belongs to that platform's issue — and separate from
:mod:`apps.channels.providers.messenger` because the adapter is the thing Layer 5
copies and a screen is not part of SPEC §6.1. The Graph calls this flow makes live
in :mod:`apps.channels.oauth_meta`; this module owns what an operator sees.

Three routes and one round trip:

``messenger/connect/``
    Explains what is about to happen and what it needs. ``POST`` mints a signed
    ``state`` and sends the browser to Facebook.

``/oauth/meta/callback/``
    Where Facebook comes back. **Not** under ``/w/<workspace_id>/`` — Meta
    whitelists one exact redirect URI per app, and a per-workspace path would
    mean one whitelist entry per tenant. The workspace comes from the signed
    state instead, and the signed-in user's permission on *that* workspace is
    checked before anything else happens.

``messenger/pages/``
    Pick which page to connect. ``POST`` writes the connection, subscribes the
    webhook fields and configures the Get Started button.

--------------------------------------------------------------------------
What is held between the two halves, and how
--------------------------------------------------------------------------

The callback ends holding a long-lived **user** access token, and the page the
operator has not chosen yet needs it. So it is carried in the session — which is
database-backed and server-side here (``SESSION_ENGINE`` is the db backend), never
a cookie — and it is carried **encrypted**, with the same AES-256-GCM utility
every credential column uses, because SECURITY-BASELINE §5 says tokens live in
encrypted fields and ``django_session.session_data`` is a plain column.

It is deleted the moment a page is chosen, and it expires on its own
(:data:`PENDING_MAX_AGE`) so an abandoned attempt does not leave a live token in a
row nobody looks at. **Page** tokens are never held at all: the chooser re-reads
``/me/accounts`` on the POST, so the credential that ends up on the connection has
existed only inside one request.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.channels import oauth_meta
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection
from apps.channels.providers import messenger as messenger_adapter
from apps.channels.providers import meta_common
from apps.channels.providers.exceptions import APIError
from apps.common.encryption import decrypt_value, encrypt_value
from apps.common.platforms import Platform
from apps.credentials.resolution import resolve_platform_credentials
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["messenger_connect", "messenger_oauth_callback", "messenger_pages"]

#: Where the half-finished connect attempt lives on the session.
PENDING_SESSION_KEY = "messenger_connect"

#: How long a half-finished attempt stays usable, in seconds. Matches the state's
#: own lifetime: an operator who wandered off mid-flow starts again rather than
#: finishing with a token minted twenty minutes ago.
PENDING_MAX_AGE = oauth_meta.STATE_MAX_AGE

#: Shown for every reason the OAuth round trip can fail: a tampered state, an
#: expired one, a declined consent screen, a code Facebook will not exchange.
#:
#: **Deliberately one message.** The operator's next step is the same in all of
#: them — start again — and a message that distinguished them would tell whoever
#: sent a forged link which part of the forgery was wrong.
OAUTH_FAILED = (
    "Facebook did not complete that connection. Start the setup again from this page, and make sure "
    "you accept every permission it asks for."
)

#: Shown when the deployment has no Meta app configured. Distinct from the above
#: on purpose: this one is not a failed attempt, it is a missing prerequisite, and
#: the fix is a different screen.
NOT_CONFIGURED = (
    "This workspace has no Facebook app credentials yet. Add them under Settings → Credentials, "
    "or set PLATFORM_MESSENGER_CLIENT_ID and PLATFORM_MESSENGER_CLIENT_SECRET; see "
    "docs/channels/messenger.md."
)

#: Shown when the page is good but Meta would not subscribe it. Separate because
#: the fix is genuinely different — almost always a missing permission or an app
#: still in development mode.
SUBSCRIBE_FAILED = (
    "That page connected, but Facebook would not start sending its messages here. The app needs "
    "pages_messaging and pages_manage_metadata on this page; see docs/channels/messenger.md."
)


# ---------------------------------------------------------------------------
# Step 1 — start
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def messenger_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Explain the setup, and start it (SPEC §6.4).

    ``GET`` renders; ``POST`` sends the operator to Facebook. Starting on a POST
    rather than a link is what puts Django's CSRF token on the *outbound* leg —
    the state parameter protects the return leg, and the two are different
    problems.
    """
    credentials = _app_credentials(request.workspace)
    if request.method == "POST":
        if not credentials:
            messages.error(request, NOT_CONFIGURED)
            return redirect(request.path)
        return redirect(
            oauth_meta.authorize_url(
                client_id=credentials["client_id"],
                state=oauth_meta.mint_state(request.workspace.pk),
            )
        )

    return render(
        request,
        "channels/messenger_connect.html",
        {
            "configured": bool(credentials),
            "not_configured_message": NOT_CONFIGURED,
            "scopes": oauth_meta.SCOPES,
            "subscribed_fields": messenger_adapter.SUBSCRIBED_FIELDS,
            # The URL Meta has to have whitelisted, shown so an operator can copy
            # it into the app console rather than guess at it.
            "callback_url": oauth_meta.callback_url(),
            "credentials_url": reverse("credentials:list", kwargs={"workspace_id": workspace_id}),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


# ---------------------------------------------------------------------------
# Step 2 — Facebook comes back
# ---------------------------------------------------------------------------


@login_required
@require_http_methods(["GET"])
def messenger_oauth_callback(request: HttpRequest) -> HttpResponse:
    """Where Facebook Login for Business returns (issue #18).

    Carries no workspace in its path — see the module docstring — so it does its
    own tenancy work, in this order and for these reasons:

    1. **The state**, first, because everything after it costs something. A
       missing, forged, expired or foreign-purpose state is refused before a
       query is run.
    2. **The membership**, because a valid state proves *we* minted it for a
       workspace, not that the person holding it belongs there. Without this
       check, a state captured from an admin's browser would let anyone attach
       their own Facebook page to that workspace.
    3. **The permission**, the same ``manage_channels`` the other two views are
       decorated with. Spelled out here rather than decorated because the
       decorator reads a membership the middleware resolves from the URL, and
       this URL has none.

    A cross-tenant answer is ``404`` and a within-tenant permission failure is
    ``403``, exactly as ``RBACMiddleware`` and ``require_permission`` would have
    answered (SECURITY-BASELINE §1, CONTRIBUTING).
    """
    workspace_id = oauth_meta.read_state(request.GET.get("state", ""))
    if not workspace_id:
        logger.info("Messenger connect: a callback arrived with an unusable state.")
        raise Http404("No such connection attempt.")

    membership = _membership_or_404(request.user, workspace_id)
    if not membership.effective_permissions.get("manage_channels", False):
        raise PermissionDenied("Permission denied: manage_channels")
    workspace = membership.workspace

    pages_url = reverse("channels:messenger_pages", kwargs={"workspace_id": str(workspace.pk)})
    connect_url = reverse("channels:messenger_connect", kwargs={"workspace_id": str(workspace.pk)})

    code = request.GET.get("code", "")
    if not code or request.GET.get("error"):
        # The operator pressed Cancel, or declined a permission. Not an error
        # worth a stack trace, and the same wording as every other failure.
        messages.error(request, OAUTH_FAILED)
        return redirect(connect_url)

    credentials = _app_credentials(workspace)
    if not credentials:
        messages.error(request, NOT_CONFIGURED)
        return redirect(connect_url)

    try:
        user_token = oauth_meta.exchange_code(
            code=code,
            client_id=credentials["client_id"],
            client_secret=credentials["client_secret"],
        )
    except APIError:
        # No exception detail in the message and none in the log: the surrounding
        # code path is the one place an app secret exists in plain text
        # (SECURITY-BASELINE §5).
        logger.info("Messenger connect: the code exchange was refused for workspace %s.", workspace.pk)
        messages.error(request, OAUTH_FAILED)
        return redirect(connect_url)

    request.session[PENDING_SESSION_KEY] = {
        "workspace": str(workspace.pk),
        # Encrypted, because ``django_session.session_data`` is a plain column and
        # SECURITY-BASELINE §5 puts every token in an encrypted field.
        "token": encrypt_value(user_token),
        "at": timezone.now().timestamp(),
    }
    return redirect(pages_url)


# ---------------------------------------------------------------------------
# Step 3 — choose a page
# ---------------------------------------------------------------------------


@login_required
@require_permission("manage_channels")
@require_http_methods(["GET", "POST"])
def messenger_pages(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """List the pages this Facebook account granted, and connect the chosen one.

    Both methods re-read ``/me/accounts``. That is one extra Graph call on the
    POST, and it buys the property that a **page** access token — the credential
    that *is* the page — never exists anywhere but inside a single request: not in
    the session, not in a hidden form field, not in a log.
    """
    token = _pending_token(request, workspace_id)
    if not token:
        messages.error(request, OAUTH_FAILED)
        return redirect(reverse("channels:messenger_connect", kwargs={"workspace_id": workspace_id}))

    try:
        pages = oauth_meta.list_pages(token)
    except APIError:
        logger.info("Messenger connect: listing pages failed for workspace %s.", request.workspace.pk)
        _clear_pending(request)
        messages.error(request, OAUTH_FAILED)
        return redirect(reverse("channels:messenger_connect", kwargs={"workspace_id": workspace_id}))

    error = ""
    if request.method == "POST":
        chosen = next((page for page in pages if page.id == (request.POST.get("page_id") or "").strip()), None)
        if chosen is None:
            # Either nothing was picked, or a hand-crafted POST named a page this
            # account does not administer. One answer for both: naming which
            # would confirm whether a given page id is real.
            error = "Pick one of the pages below."
        else:
            error = _connect_page(request, chosen)
            if not error:
                _clear_pending(request)
                return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/messenger_pages.html",
        {
            # Ids and names only. ``MetaPage.access_token`` is never rendered,
            # and the template has no field that could echo one back.
            "pages": [{"id": page.id, "name": page.name} for page in pages],
            "error": error,
            "connect_url": reverse("channels:messenger_connect", kwargs={"workspace_id": workspace_id}),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _connect_page(request: WorkspaceRequest, page: oauth_meta.MetaPage) -> str:
    """Write the connection and wire the page up. "" on success, else a message.

    The order is ``views_telegram._connect``'s, and load-bearing for the same
    reasons. The row is written first, inside its own savepoint, because an
    ``IntegrityError`` caught without one poisons the request's transaction and
    turns a duplicate page into a 500 instead of a form error.

    Then ``subscribed_apps`` and the Get Started button run **outside** that
    savepoint: they are network round trips with a 30-second timeout, and holding
    a transaction open across them would pin a database connection from the pool
    while Meta is degraded. The cost is that the failure path deletes rather than
    rolls back — which is right, because a page Meta will not deliver for is not a
    connection, and one left in the list looking connected while nothing ever
    arrives is the worse outcome.
    """
    connection = ChannelConnection(
        workspace=request.workspace,
        platform=Platform.MESSENGER.value,
        display_name=page.name[:200],
        external_id=page.id,
    )
    meta_common.store_page_token(connection, page.access_token)
    # Minted so the connection has one like every other row, and so rotating it
    # from the settings page is not a special case. Messenger never presents it:
    # Meta signs with the app secret (see ``MessengerAdapter.verify_webhook``).
    connection.rotate_webhook_secret()

    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # SPEC §5's unique (platform, external_id) is deployment-wide, so this can
        # be another workspace's row. The wording never says which
        # (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    try:
        messenger_adapter.subscribe_page(connection)
    except APIError:
        logger.info("Messenger connect: subscribing page failed for workspace %s.", request.workspace.pk)
        connection.delete()
        return SUBSCRIBE_FAILED

    try:
        messenger_adapter.set_get_started(connection)
    except APIError:
        # Not fatal, unlike the subscription. Everything except SPEC §10's welcome
        # trigger works without it, and deleting a working channel over a button
        # would be the wrong trade — so the connection stands and the operator is
        # told what is missing.
        logger.info("Messenger connect: the Get Started button could not be set for %s.", connection.pk)
        messages.warning(
            request,
            f"Connected {connection.display_name}, but the Get Started button could not be configured — "
            f"the welcome trigger will not fire until it is. See docs/channels/messenger.md.",
        )
        return ""

    messages.success(request, f"Connected {connection.display_name}. Send it a message to check it works.")
    return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_credentials(workspace: Any) -> dict[str, str]:
    """The Meta app id and secret in force for ``workspace``, or {}.

    SPEC §4's chain — workspace override, then organization, then the
    deployment's environment — through ``apps.credentials.resolution``. Meta's own
    documentation says ``app_id``/``app_secret`` while its OAuth endpoints say
    ``client_id``/``client_secret``; ``REQUIRED_CREDENTIAL_KEYS`` accepts both, so
    both are read and the OAuth spelling is what comes back.
    """
    resolution = resolve_platform_credentials(Platform.MESSENGER.value, workspace=workspace)
    if not resolution:
        return {}
    values = resolution.credentials
    client_id = _first(values, ("client_id", "app_id"))
    client_secret = _first(values, ("client_secret", "app_secret"))
    if not client_id or not client_secret:
        return {}
    return {"client_id": client_id, "client_secret": client_secret}


def _first(values: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _membership_or_404(user: Any, workspace_id: str) -> WorkspaceMembership:
    """The signed-in user's membership of ``workspace_id``, or 404.

    404 rather than 403 for a workspace they do not belong to: a 403 would confirm
    the id names a real workspace, which over a UUID space is the only thing an
    attacker was missing (SECURITY-BASELINE §1, CONTRIBUTING). Archived workspaces
    are a miss too, the way ``RBACMiddleware._membership_for`` treats them.
    """
    try:
        membership = (
            WorkspaceMembership.objects.filter(
                user=user,
                workspace_id=workspace_id,
                workspace__is_archived=False,
            )
            .select_related("workspace__organization")
            .first()
        )
    except (ValueError, TypeError):
        membership = None
    if membership is None:
        raise Http404("No such workspace.")
    return membership


def _pending_token(request: HttpRequest, workspace_id: str) -> str:
    """The stashed user access token for this workspace's attempt, or "".

    Three ways to get nothing back, all answered identically by the caller: there
    is no attempt, the attempt belongs to another workspace (a second tab, or a
    URL edited by hand), or it is older than :data:`PENDING_MAX_AGE`.
    """
    pending = request.session.get(PENDING_SESSION_KEY)
    if not isinstance(pending, dict) or pending.get("workspace") != str(workspace_id):
        return ""
    started = pending.get("at")
    if not isinstance(started, (int, float)) or timezone.now().timestamp() - started > PENDING_MAX_AGE:
        _clear_pending(request)
        return ""
    token = pending.get("token")
    if not isinstance(token, str) or not token:
        return ""
    try:
        return decrypt_value(token)
    except ValueError:
        # A deployment whose encryption key changed mid-flow. Nothing to recover;
        # the operator starts again.
        _clear_pending(request)
        return ""


def _clear_pending(request: HttpRequest) -> None:
    """Drop the stashed token. Called on success and on every failure path."""
    if request.session.pop(PENDING_SESSION_KEY, None) is not None:
        request.session.modified = True
