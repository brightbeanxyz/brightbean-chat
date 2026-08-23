"""Telegram's connect flow and the builder's "test on Telegram" link (issue #12).

Separate from :mod:`apps.channels.views` because the frame and the flow are
different things. ``views.py`` ships what every platform shares — the connection
row, its status, its webhook URL and secret — and issue #4's docstring is
explicit that each platform's real connect flow belongs to that platform's own
issue, because "a BotFather token pasted into a field, a Meta OAuth round trip,
Twilio credentials plus a number" are not one form.

Separate from :mod:`apps.channels.providers.telegram` because the adapter is the
thing Layer 5 copies and a view is not part of it. The adapter owns the Bot API;
this module owns what an operator sees.

Two routes:

``telegram/connect/``
    Paste a token, and we do the rest: ``getMe`` to prove it and learn the bot's
    identity, then a connection row, then ``setWebhook`` with a freshly minted
    secret. Nothing is stored until the token is known good.

``telegram/preview/<flow_id>/``
    Mint a preview deep link for the flow builder (SPEC §16). Answers JSON,
    because its caller is the React island rather than a browser navigation.
"""

import logging
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.channels import preview
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import PREVIEW_LINK_TTL, ChannelConnection, ConnectionStatus
from apps.channels.providers import telegram
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows.models import Flow
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["telegram_connect", "telegram_preview"]

#: Shown when the pasted token does not work. Deliberately one message for every
#: reason ``getMe`` can fail — wrong token, revoked token, Telegram unreachable.
#: An operator's next step is the same in all three (check it with BotFather and
#: try again), and a message that distinguished them would be an oracle for
#: whether a given token string is a real bot.
REJECTED_MESSAGE = (
    "Telegram did not accept that token. Copy it again from BotFather — it looks like "
    "123456789:AA... — and make sure the bot has not been revoked."
)

#: Telegram usernames: 5-32 characters of ``[A-Za-z0-9_]``. Checked before a
#: deep link is built from one, because the generic "Add a channel" form can
#: still create a Telegram connection with any display name at all — and a
#: ``t.me/My bot?start=…`` link would be a broken link the tester has no way to
#: diagnose.
BOT_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")

#: Shown when the token is good but ``setWebhook`` failed. Separate from the
#: above because the fix is genuinely different: this one is almost always a
#: deployment that is not reachable over public HTTPS.
WEBHOOK_FAILED = (
    "That token works, but Telegram could not be pointed at this deployment. The webhook "
    "URL has to be reachable over public HTTPS with a valid certificate; see "
    "docs/channels/telegram.md."
)


@login_required
@require_permission("manage_channels")
def telegram_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Connect a Telegram bot from its BotFather token (SPEC §6.2).

    The order is load-bearing. ``getMe`` comes **first**, before anything is
    written, because it is the only thing that can tell us the bot's id and
    username — the ``external_id`` and display name the row needs — and because
    a token that does not work should leave no trace. ``setWebhook`` comes last
    and inside the transaction: if Telegram will not accept the webhook, the
    connection is rolled back rather than left sitting in the list looking
    connected while nothing is ever delivered to it.

    The token itself is never rendered back, not even into the form on a failed
    submit (CONTRIBUTING: "never render a stored secret", and this one is not
    even stored yet).
    """
    error = ""
    if request.method == "POST":
        token = (request.POST.get("bot_token") or "").strip()
        if not token:
            error = "Paste the token BotFather gave you."
        else:
            error = _connect(request, token)
            if not error:
                return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/telegram_connect.html",
        {
            "error": error,
            # The URL the connect actually configures, not one derived from
            # this request — so what the page promises and what Telegram is told
            # cannot disagree on a deployment behind a proxy.
            "webhook_url": telegram.webhook_url(),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _connect(request: WorkspaceRequest, token: str) -> str:
    """Do the connect. Returns "" on success or the message to show."""
    try:
        bot = telegram.get_me(token)
    except APIError:
        # No exception detail in the message and none in the log: an APIError
        # from `request_json` names the host, but the surrounding code path is
        # the one place a bot token exists in plain text (SECURITY-BASELINE §5).
        logger.info("Telegram connect: getMe was rejected for workspace %s.", request.workspace.pk)
        return REJECTED_MESSAGE

    bot_id = bot.get("id")
    username = bot.get("username")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or not isinstance(username, str) or not username:
        logger.warning("Telegram connect: getMe returned an unusable identity.")
        return REJECTED_MESSAGE

    connection = ChannelConnection(
        workspace=request.workspace,
        platform=Platform.TELEGRAM.value,
        display_name=f"@{username}"[:200],
        external_id=str(bot_id),
    )
    telegram.store_bot_token(connection, token)
    secret = connection.rotate_webhook_secret()

    try:
        # The savepoint is not optional and is not about setWebhook: an
        # IntegrityError marks the surrounding transaction unusable, so without
        # atomic() here a duplicate bot would poison every query for the rest of
        # the request rather than becoming the form error below. Same reason
        # ``views.connection_create`` wraps its insert.
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # SPEC §5's unique (platform, external_id) is deployment-wide, so this
        # can be another workspace's row. The wording never says which — same
        # message the form's own pre-check uses (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    # setWebhook runs **outside** the savepoint above, and deliberately. It is
    # a network round trip with a 30-second timeout; holding a transaction open
    # across it would pin a database connection from the pool for that long,
    # and a handful of operators connecting bots while Telegram is degraded
    # would exhaust the pool and take unrelated requests down with it.
    #
    # The cost of moving it out is that the row exists for the duration, so the
    # failure path has to clean up rather than roll back. It deletes: a bot
    # Telegram will not deliver to is not a connection, and one left in the list
    # looking connected while nothing ever arrives is the worse outcome. A
    # delete that itself fails leaves a row the operator can remove by hand,
    # which is recoverable; a wedged pool is not.
    try:
        telegram.set_webhook(token, url=telegram.webhook_url(), secret_token=secret)
    except APIError:
        logger.info("Telegram connect: setWebhook failed for workspace %s.", request.workspace.pk)
        connection.delete()
        return WEBHOOK_FAILED

    messages.success(request, f"Connected @{username}. Send it /start to check it works.")
    return ""


@login_required
@require_permission("edit_flows")
@require_POST
def telegram_preview(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Mint a "test on Telegram" deep link for one flow (SPEC §16).

    ``edit_flows`` rather than ``manage_channels``: this is the builder's Test
    button, and the person pressing it is a flow author who may well not
    administer channels. It reads a connection but changes nothing about it.

    The empty state is a **200 with a reason**, not an error status. The caller
    is the builder island, "you have no Telegram bot connected yet" is an
    ordinary thing for it to render, and a 4xx would send it down its API-error
    path and show a failure instead of an explanation.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)

    connection = (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(platform=Platform.TELEGRAM.value, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
        .first()
    )
    if connection is None:
        return JsonResponse(
            {
                "ok": False,
                "reason": "no_connection",
                "message": "Connect a Telegram bot first — testing runs the draft in a real chat with it.",
                "settings_url": reverse("channels:telegram_connect", kwargs={"workspace_id": workspace_id}),
            }
        )

    username = connection.display_name.lstrip("@")
    if not BOT_USERNAME.match(username):
        return JsonResponse(
            {
                "ok": False,
                "reason": "no_username",
                "message": (
                    "That Telegram connection has no usable bot username. Reconnect it through the "
                    "guided setup so its username comes from Telegram."
                ),
                "settings_url": reverse("channels:telegram_connect", kwargs={"workspace_id": workspace_id}),
            }
        )

    link, handle = preview.mint(flow=flow, connection=connection, user=request.user)
    logger.info("Preview link %s minted for flow %s.", link.pk, flow.pk)
    return JsonResponse(
        {
            "ok": True,
            "deep_link": telegram.deep_link(username, preview.start_payload(handle)),
            "bot": f"@{username}",
            "expires_in": int(PREVIEW_LINK_TTL.total_seconds()),
        }
    )
