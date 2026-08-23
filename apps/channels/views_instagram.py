"""Instagram's guided connect flow and the comment trigger's post picker (issue #17).

Separate from :mod:`apps.channels.views` because the frame and the flow are
different things — that module ships the connection row, its status and its
webhook URL, and issue #4's docstring is explicit that each platform's real
connect flow belongs to that platform's own issue.

Separate from :mod:`apps.channels.providers.instagram` because the adapter is the
thing four more platforms copy and a view is not part of it. The adapter owns the
Graph API; this module owns what an operator sees. The token exchange itself is a
third thing again, in :mod:`apps.channels.instagram_oauth`.

Three routes:

``instagram/connect/``
    Workspace-scoped, ``manage_channels``. Explains what will happen, then sends
    the operator to Meta with a signed ``state``.

``/channels/instagram/callback/``
    **Not** workspace-scoped, and it cannot be: Meta matches a redirect URI
    against the app's configuration exactly, so one app has one callback URL for
    the whole deployment. The workspace travels in the signed ``state`` and is
    re-authorised here, against the signed-in user, before anything is exchanged
    or written.

``instagram/posts/``
    The post picker for SPEC §10's comment trigger. ``edit_flows`` rather than
    ``manage_channels``, for the same reason ``telegram_preview`` is: the person
    using it is a flow author who may well not administer channels, and it reads
    the connection without changing anything about it.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.channels import instagram_oauth as oauth
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers import instagram
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.models import WorkspaceMembership
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["instagram_callback", "instagram_connect", "instagram_posts"]

#: Shown when this workspace has no Meta app credentials to start an OAuth flow
#: with. Names the two ways to supply them and neither of the values.
NO_CREDENTIALS = (
    "This workspace has no Instagram app credentials yet. Add them under Settings -> "
    "Credentials, or set PLATFORM_INSTAGRAM_CLIENT_ID and PLATFORM_INSTAGRAM_CLIENT_SECRET "
    "on the deployment. See docs/channels/instagram.md."
)

#: Shown when Meta refuses the code, the token exchange fails, or the profile
#: comes back unusable. Deliberately one message for every reason: the operator's
#: next step is the same in all of them (start again), and distinguishing them
#: would be an oracle for which app ids and codes are real.
REJECTED_MESSAGE = (
    "Instagram did not complete the connection. Start again from Settings -> Channels, "
    "and check that this deployment's callback URL is listed in your Meta app."
)

#: Shown when the ``state`` does not verify. Its own message because the fix is
#: genuinely different — an expired state means "you left the tab open", and it
#: is the only one of these an operator can cause by being slow.
STATE_REJECTED = (
    "That connection attempt could not be verified, so nothing was changed. This usually "
    "means it took more than ten minutes, or the link was not started from this app. "
    "Please start again."
)


@login_required
@require_permission("manage_channels")
def instagram_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Start the OAuth round trip for an Instagram professional account.

    ``GET`` explains what is about to happen; ``POST`` redirects to Meta. A
    redirect on POST rather than a link on the page, so the ``state`` is minted
    at the moment the operator acts rather than whenever the page happened to be
    rendered — a page left open overnight would otherwise carry a state that
    expires before it is used, which reads as a bug rather than as a timeout.
    """
    error = ""
    if request.method == "POST":
        try:
            client_id, _ = oauth.app_credentials(request.workspace)
        except oauth.InstagramCredentialsMissingError:
            error = NO_CREDENTIALS
        else:
            state = oauth.sign_state(workspace_id=request.workspace.pk, user_id=request.user.pk)
            return redirect(oauth.authorize_url(client_id=client_id, state=state))

    return render(
        request,
        "channels/instagram_connect.html",
        {
            "error": error,
            "callback_url": oauth.callback_url(),
            "scopes": oauth.SCOPES,
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


@login_required
@require_GET
def instagram_callback(request: Any) -> HttpResponse:
    """Meta's redirect back. Verifies ``state`` before touching anything.

    The order is the security property. ``state`` is checked **first**, then the
    signed-in user is compared against the one it was minted for, then that
    user's permission in the workspace it names — and only then is the code
    exchanged. Without that, anyone who could make an operator's browser land
    here with a code of their own would connect *their* Instagram account into
    the operator's workspace, and every DM to that account would start arriving
    in a stranger's inbox.

    A failed state answers **404**, not 403: this route is reachable without
    knowing anything about the deployment, and a distinguishable refusal would
    confirm that a workspace id or a user id names something real
    (SECURITY-BASELINE §1, and the same rule ``unsign_or_404`` applies).
    """
    payload = oauth.read_state(request.GET.get("state", ""))
    if payload is None:
        logger.info("Instagram callback: the state parameter did not verify.")
        raise Http404(STATE_REJECTED)

    if str(request.user.pk) != payload["u"]:
        # The state was minted for somebody else. A shared machine, a stale tab,
        # or an attempt to graft a connection onto another person's session.
        logger.warning("Instagram callback: the state was minted for a different user.")
        raise Http404(STATE_REJECTED)

    membership = _membership(request.user, payload["ws"])
    if membership is None or not membership.effective_permissions.get("manage_channels", False):
        logger.warning("Instagram callback: the signed-in user may not manage channels in that workspace.")
        raise Http404(STATE_REJECTED)

    workspace = membership.workspace
    settings_url = reverse("channels:list", kwargs={"workspace_id": workspace.pk})

    error = request.GET.get("error_reason") or request.GET.get("error")
    if error:
        # The operator pressed Cancel on Meta's screen, or Meta refused. Neither
        # is our failure and neither needs a scary page.
        messages.info(request, "Instagram was not connected.")
        return redirect(settings_url)

    code = (request.GET.get("code") or "").strip()
    if not code:
        raise Http404(STATE_REJECTED)

    problem = _complete(request, workspace, code)
    if problem:
        messages.error(request, problem)
    return redirect(settings_url)


def _membership(user: Any, workspace_id: str) -> WorkspaceMembership | None:
    """The user's membership in the workspace the state names, or None.

    Resolved here rather than by ``RBACMiddleware`` because this route carries no
    ``workspace_id`` kwarg — see the module docstring on why it cannot. The
    permission decision still goes through ``effective_permissions``, which
    ``apps.members.models`` documents as the only place one is ever made.

    A malformed id is a miss rather than a 500: it arrives from a signed payload,
    but the row it names can have been deleted since.
    """
    try:
        return (
            WorkspaceMembership.objects.filter(
                user=user,
                workspace_id=workspace_id,
                workspace__is_archived=False,
            )
            .select_related("workspace__organization")
            .first()
        )
    except (ValueError, TypeError):
        return None


def _complete(request: Any, workspace: Any, code: str) -> str:
    """Exchange the code and store the connection. "" on success, else a message.

    The order is load-bearing, and mirrors ``views_telegram._connect``. The token
    exchanges and the profile read come **first**, before anything is written,
    because they are the only things that can tell us which account this is — the
    ``external_id`` every inbound webhook is resolved by — and because a code
    that does not work should leave no trace.
    """
    try:
        client_id, client_secret = oauth.app_credentials(workspace)
    except oauth.InstagramCredentialsMissingError:
        return NO_CREDENTIALS

    try:
        short_lived, _ = oauth.exchange_code(code=code, client_id=client_id, client_secret=client_secret)
        token, expires_at = oauth.exchange_for_long_lived(token=short_lived, client_secret=client_secret)
        profile = oauth.account_profile(token)
    except APIError:
        # No exception detail in the message and none in the log: this code path
        # is one of the few places a live token exists in plain text, and an
        # APIError's text names the host it came from (SECURITY-BASELINE §5).
        logger.info("Instagram connect: the token exchange failed for workspace %s.", workspace.pk)
        return REJECTED_MESSAGE

    connection = ChannelConnection(
        workspace=workspace,
        platform=Platform.INSTAGRAM.value,
        display_name=f"@{profile['username']}"[:200],
        external_id=profile["user_id"],
        status=ConnectionStatus.ACTIVE,
    )
    oauth.store_credentials(connection, token=token, expires_at=expires_at, user_id=profile["user_id"])

    try:
        # The savepoint is not optional: an IntegrityError marks the surrounding
        # transaction unusable, so without atomic() here a duplicate account
        # would poison every query for the rest of the request rather than
        # becoming the message below. Same call ``views.connection_create`` makes.
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        existing = _reconnect(workspace, profile, token, expires_at)
        if existing is not None:
            messages.success(request, f"Reconnected {connection.display_name}.")
            return ""
        # SPEC §5's unique (platform, external_id) is deployment-wide, so this
        # can be another workspace's row. The wording never says which
        # (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    messages.success(
        request,
        f"Connected {connection.display_name}. Subscribe your Meta app to this account's "
        f"webhook fields to start receiving messages — see docs/channels/instagram.md.",
    )
    return ""


def _reconnect(workspace: Any, profile: dict[str, str], token: str, expires_at: Any) -> ChannelConnection | None:
    """Refresh **this workspace's** existing row for the same account, if any.

    Reconnecting is the ordinary way out of ``needs_reauth``, and it has to work
    without the operator deleting the connection first — deleting would take its
    conversations, triggers and identities with it.

    Scoped to the workspace, so a row belonging to somebody else is not found and
    the caller falls through to the "already connected" message. That is the
    whole tenancy check on this path: without it, connecting an account another
    workspace holds would hand its token — and its inbound traffic — to whoever
    connected second.
    """
    connection = (
        ChannelConnection.objects.for_workspace(workspace)
        .filter(platform=Platform.INSTAGRAM.value, external_id=profile["user_id"])
        .first()
    )
    if connection is None:
        return None
    oauth.store_credentials(connection, token=token, expires_at=expires_at, user_id=profile["user_id"])
    connection.display_name = f"@{profile['username']}"[:200]
    connection.status = ConnectionStatus.ACTIVE
    connection.save(update_fields=["credentials", "display_name", "status", "updated_at"])
    logger.info("Instagram connection %s was reconnected.", connection.pk)
    return connection


@login_required
@require_permission("edit_flows")
@require_GET
def instagram_posts(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Recent posts, for the comment trigger's post picker (SPEC §10).

    The empty and error states are **200s with a reason**, not error statuses.
    The caller is an HTMX fragment inside the trigger drawer; a 4xx would send it
    down its error path and show a failure where "connect an Instagram account
    first" is an ordinary thing to render.
    """
    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )
    context: dict[str, Any] = {"posts": [], "reason": "", "connect_url": ""}
    if connection is None:
        context["reason"] = "Connect an Instagram account to pick posts from it."
        context["connect_url"] = reverse("channels:instagram_connect", kwargs={"workspace_id": workspace_id})
        return render(request, "channels/_instagram_posts.html", context)

    try:
        context["posts"] = instagram.recent_media(connection)
    except APIError:
        logger.info("Instagram post picker: /me/media was refused for connection %s.", connection.pk)
        context["reason"] = "Instagram would not list this account's posts. Reconnect the channel and try again."
    except Exception:
        logger.exception("Instagram post picker failed for connection %s.", connection.pk)
        context["reason"] = "Instagram's posts could not be loaded just now."
    if not context["posts"] and not context["reason"]:
        context["reason"] = "This account has no posts yet."
    return render(request, "channels/_instagram_posts.html", context)
