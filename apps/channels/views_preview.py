"""Testing a draft on the channel it was built for (SPEC §16, extended).

SPEC §16 scoped the preview to Telegram — "cheapest real-channel test loop" —
and that is what shipped. The cost showed up the first time somebody built an
Instagram comment-to-DM automation: the only way to see their own flow was to
publish it to real customers.

The mechanism generalises cleanly to the two other platforms that can carry a
``ref`` into a referral event, and everything downstream of the link was already
platform-agnostic: ``preview._claim`` binds on the connection, and ``chat_id``
holds a PSID or an IGSID as happily as a Telegram chat id.

It does **not** generalise to WhatsApp, SMS or email, and this module does not
pretend otherwise. See ``preview.PREVIEW_LINKS`` for why, and note that the
honest answer — an explained empty state naming the platform — is a better
outcome than a token the tester has to paste into a message body.

Which platform is offered comes from the flow's own triggers, which is what
"the channel you built it for" means. A flow with only an Instagram trigger is
not handed a Telegram link just because a bot happens to be connected.
"""

import logging
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_POST

from apps.channels import preview
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.registry import connect_route_for
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.flows.models import Flow
from apps.flows.triggers.platforms import declared_platforms_for_flow
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

__all__ = ["flow_preview"]

logger = logging.getLogger(__name__)


@login_required
@require_permission("edit_flows")
@require_POST
def flow_preview(request: WorkspaceRequest, workspace_id: str, flow_id: str) -> HttpResponse:
    """Mint a deep link that runs this flow's draft in a real chat.

    ``edit_flows`` rather than ``manage_channels``: this is the builder's Test
    button, pressed by a flow author who may well not administer channels. It
    reads a connection and changes nothing about it.

    Every empty state is a **200 with a reason**, never a 4xx. The caller is the
    builder island, "you have no Instagram account connected yet" is an ordinary
    thing for it to render, and a 4xx would send it down its API-error path and
    show a failure instead of an explanation.
    """
    flow = get_scoped_object_or_404(Flow, request.workspace, pk=flow_id)

    # declared_platforms_for_flow, not platforms_for_flow: the latter is the
    # capability validator's input and falls back to whatever the workspace has
    # connected, which would hand this view a channel the flow never mentioned.
    # An Instagram flow in a Telegram-only workspace has to be told to connect
    # Instagram, not offered a Telegram chat.
    wanted = declared_platforms_for_flow(flow)
    testable = _testable(wanted)
    if not testable:
        return JsonResponse(_unsupported(wanted))

    unusable = ""
    for platform in testable:
        connection, handle, problem = _connection_for(request, platform)
        if connection is None:
            # Remember the most specific complaint, but keep looking: another
            # platform this flow runs on may have a connection that works.
            unusable = unusable or problem
            continue
        link, token = preview.mint(flow=flow, connection=connection, user=request.user)
        logger.info("Preview link %s minted for flow %s on %s.", link.pk, flow.pk, platform)
        return JsonResponse(
            {
                "ok": True,
                "platform": platform,
                "platform_label": str(dict(Platform.choices).get(platform, platform)),
                "deep_link": preview.preview_link(connection, handle, preview.start_payload(token)),
                "account": _account_label(connection, handle),
                "instructions": preview.PREVIEW_INSTRUCTIONS.get(platform, ""),
                "expires_in": int(preview.PREVIEW_LINK_TTL.total_seconds()),
            }
        )

    return JsonResponse(_no_connection(workspace_id, testable, unusable))


#: Preference order when the flow itself does not narrow things down. Telegram
#: first because SPEC §16 picked it for a reason that still holds: it is the
#: cheapest loop — tapping the link is the whole test, with no message to send.
_PREFERENCE: tuple[str, ...] = (Platform.TELEGRAM, Platform.MESSENGER, Platform.INSTAGRAM)


def _testable(wanted: tuple[str, ...]) -> list[str]:
    """The platforms worth trying, in the order to try them.

    ``declared_platforms_for_flow`` returns nothing when the flow names no
    channel of its own — a channel-independent trigger, or no trigger yet. That
    is "we cannot tell", not "this flow runs on a channel with no live test", so
    it means try them all rather than refuse. Conflating the two told a
    brand-new workspace its flow was untestable.

    A non-empty ``wanted`` is the flow's own answer and is honoured exactly: if
    none of it is testable the caller says so, and if it is testable but
    unconnected the caller names the channel to connect.
    """
    if not wanted:
        return list(_PREFERENCE)
    return [platform for platform in _PREFERENCE if platform in wanted]


def _connection_for(request: WorkspaceRequest, platform: str) -> tuple[Any, str, str]:
    """The first active connection of this platform that can actually be linked.

    Returns the reason when there is none, because the two reasons want
    different advice: "connect one" is no help to somebody who has connected
    one and whose row simply has no usable handle on it — they need to be told
    to reconnect through the guided setup, which is where a real @name comes
    from.

    Python-side rather than a queryset filter, because what makes a connection
    usable is not a column: a Telegram row created through the generic form has
    no token and would mint a link to a bot that answers nothing, and the token
    is encrypted.
    """
    seen = False
    no_handle = False
    for candidate in (
        ChannelConnection.objects.for_workspace(request.workspace)
        .filter(platform=platform, status=ConnectionStatus.ACTIVE)
        .order_by("created_at")
    ):
        if not _can_send(candidate):
            continue
        seen = True
        handle = _handle(candidate)
        if handle:
            return candidate, handle, ""
        no_handle = True
    if seen and no_handle:
        return None, "", "no_username"
    return None, "", "no_connection"


def _handle(connection: ChannelConnection) -> str:
    """This connection's public handle, per platform.

    Messenger's is its ``external_id`` — the page id, which ``m.me`` resolves —
    and the other two carry an @name in ``display_name``, written there by their
    guided connect flows. A row whose display name is something a human typed is
    not a handle, which is why Telegram's is shape-checked.
    """
    if connection.platform == Platform.MESSENGER:
        return (connection.external_id or "").strip()

    name = (connection.display_name or "").strip().lstrip("@")
    if connection.platform == Platform.TELEGRAM:
        from apps.channels.views_telegram import BOT_USERNAME

        return name if BOT_USERNAME.match(name) else ""
    return name


def _can_send(connection: ChannelConnection) -> bool:
    """Telegram needs a bot token; the Meta platforms carry their own."""
    if connection.platform != Platform.TELEGRAM:
        return True
    from apps.channels.providers import telegram

    return bool(telegram.bot_token(connection))


def _account_label(connection: ChannelConnection, handle: str) -> str:
    if connection.platform == Platform.MESSENGER:
        return connection.display_name or handle
    return f"@{handle}"


def _unsupported(wanted: tuple[str, ...]) -> dict[str, Any]:
    """No platform this flow runs on has a live test.

    Names the platform rather than saying "unsupported", because the useful
    information is *which* channel cannot be tested this way — and that it is a
    property of the channel, not something the reader has configured wrongly.
    """
    labels = dict(Platform.choices)
    named = ", ".join(str(labels.get(platform, platform)) for platform in wanted) or "this flow's channel"
    return {
        "ok": False,
        "reason": "unsupported_platform",
        "message": (
            f"There is no live test on {named}. Testing works by sending you a link that opens a real "
            "chat, and only Telegram, Messenger and Instagram carry one back."
        ),
    }


def _no_connection(workspace_id: str, testable: list[str], problem: str) -> dict[str, Any]:
    labels = dict(Platform.choices)
    platform = testable[0]
    named = str(labels.get(platform, platform))
    if problem == "no_username":
        return {
            "ok": False,
            "reason": "no_username",
            "message": (
                f"That {named} connection has no usable public name, so there is nothing to link to. "
                "Reconnect it through the guided setup so its name comes from the platform."
            ),
            "settings_url": _connect_url(workspace_id, platform),
        }
    return {
        "ok": False,
        "reason": "no_connection",
        "message": f"Connect {named} first — testing runs the draft in a real chat with it.",
        "settings_url": _connect_url(workspace_id, platform),
    }


def _connect_url(workspace_id: str, platform: str) -> str:
    """That platform's guided connect page, or the channels list.

    A route whose reverse fails costs the reader a less specific link rather
    than a 500 on the builder's Test button — the same posture
    ``views_triggers._post_pickers`` takes for the same reason.
    """
    route = connect_route_for(platform)
    for candidate in (route, "channels:list"):
        if not candidate:
            continue
        try:
            return reverse(candidate, kwargs={"workspace_id": workspace_id})
        except NoReverseMatch:
            continue
    return ""
