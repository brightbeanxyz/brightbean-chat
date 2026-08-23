"""Twilio's connect flow, the workspace's SMS settings, and the segment preview.

Separate from :mod:`apps.channels.views` because the frame and the flow are
different things, and separate from :mod:`apps.channels.providers.sms` because
the adapter is what a later platform copies and a view is not part of it — the
same split ``views_telegram`` made and for the same reasons. Issue #4's own
docstring named "Twilio credentials plus a number" as one of the three shapes a
generic form could not have collected.

Three routes:

``sms/connect/``
    Account SID, auth token, and a from-number **or** a messaging service.
    Everything is validated against Twilio before a row is written, so a wrong
    credential leaves no trace.

``sms/settings/``
    SPEC §6.6's configurable copy — the HELP reply and the two confirmations —
    plus a per-segment cost hint and the A2P 10DLC checklist. Nothing here can
    weaken the compliance behaviour; see :class:`~apps.channels.models.SmsSettings`.

``sms/segments/``
    The segment-count preview, an HTMX fragment over the pure function in
    :mod:`apps.channels.segments`. POST rather than GET because the thing being
    counted is message text, and a GET would put a draft broadcast in a URL,
    an access log and a browser history.
"""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.channels import segments
from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection, SmsSettings
from apps.channels.providers import sms
from apps.channels.providers.exceptions import APIError
from apps.common.platforms import Platform
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["sms_connect", "sms_segment_preview", "sms_settings", "sms_settings_update"]

#: Shown when Twilio will not accept the credentials. Deliberately one message
#: for every reason the check can fail — wrong SID, wrong token, suspended
#: account, Twilio unreachable. An operator's next step is the same in all of
#: them, and a message that distinguished them would be an oracle for whether a
#: given account SID exists.
REJECTED_MESSAGE = (
    "Twilio did not accept those credentials. Copy the Account SID and Auth Token "
    "again from the Twilio console — the SID looks like AC… — and check the account is active."
)

#: Shown when the credentials work but the sender does not belong to them. A
#: separate message because the fix is genuinely different: the operator is
#: signed in to the right account and typed the wrong number.
SENDER_MESSAGE = (
    "Those credentials work, but that account does not hold that number or messaging service. "
    "Check the number is in E.164 form (+15551234567), or paste the messaging service SID (MG…)."
)

#: The longest a settings field may be. Generous — a HELP reply has to fit a
#: segment or two, not a page — and the point is that it is bounded at all
#: (SECURITY-BASELINE §7).
MAX_SETTINGS_CHARS = 1600

#: The longest message body the preview will count. The composer's own limit is
#: the platform's 1600; this is the bound on an unauthenticated-adjacent parse
#: of a POST body, so it is the same number with room for a paste that is about
#: to be trimmed.
MAX_PREVIEW_CHARS = 4000


@login_required
@require_permission("manage_channels")
def sms_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Connect a Twilio number or messaging service (SPEC §6.6).

    The order is load-bearing, the same way Telegram's is. The account is
    fetched **first**, before anything is written, because it is the only thing
    that can prove the token; then the sender, because a connection that cannot
    name a valid ``From`` fails on its first send and finding that out now is
    the whole point of validating. Only then is a row created.

    Unlike Telegram there is no call *back* to Twilio to register the webhook:
    Twilio has no API-first equivalent of ``setWebhook`` that this product should
    be reaching for, and the URL is per number and pasted into the console. So
    the successful page hands the operator the exact URL to paste, and
    :func:`apps.channels.providers.sms.webhook_url` is what generates it — the
    same function :meth:`~apps.channels.providers.sms.TwilioAdapter.verify_webhook`
    checks the signature against, so the two cannot disagree.

    The auth token is never rendered back, not even into the form on a failed
    submit (CONTRIBUTING: "never render a stored secret" — and this one is not
    even stored yet).
    """
    error = ""
    form = {"account_sid": "", "from_number": "", "messaging_service_sid": ""}
    if request.method == "POST":
        form = {
            "account_sid": (request.POST.get("account_sid") or "").strip(),
            "from_number": (request.POST.get("from_number") or "").strip(),
            "messaging_service_sid": (request.POST.get("messaging_service_sid") or "").strip(),
        }
        token = (request.POST.get("auth_token") or "").strip()
        error = _connect(request, token=token, **form)
        if not error:
            return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/sms_connect.html",
        {
            "error": error,
            "form": form,
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _connect(
    request: WorkspaceRequest,
    *,
    account_sid: str,
    token: str,
    from_number: str,
    messaging_service_sid: str,
) -> str:
    """Do the connect. Returns "" on success, or the message to show."""
    if not account_sid or not token:
        return "Paste both the Account SID and the Auth Token from your Twilio console."
    if bool(from_number) == bool(messaging_service_sid):
        # Exactly one. Twilio's Messages API accepts ``From`` or
        # ``MessagingServiceSid`` and refuses both, so a row holding both would
        # be a connection whose every send is rejected.
        return "Give either a from-number or a messaging service SID — not both, and not neither."

    try:
        sms.fetch_account(account_sid, token)
    except APIError:
        # No exception detail in the message and none in the log: this code path
        # is the one place an auth token exists in plain text
        # (SECURITY-BASELINE §5), and an APIError's own string names the host.
        logger.info("SMS connect: Twilio rejected the credentials for workspace %s.", request.workspace.pk)
        return REJECTED_MESSAGE

    try:
        if messaging_service_sid:
            sms.fetch_messaging_service(account_sid, token, messaging_service_sid)
        else:
            sms.fetch_number(account_sid, token, from_number)
    except APIError:
        logger.info("SMS connect: the sender was not on the account for workspace %s.", request.workspace.pk)
        return SENDER_MESSAGE

    connection = ChannelConnection(
        workspace=request.workspace,
        platform=Platform.SMS.value,
        display_name=(from_number or messaging_service_sid)[:200],
        # SPEC §5's unique (platform, external_id) is deployment-wide, which is
        # exactly right here: one Twilio number cannot serve two workspaces, and
        # the second would silently take the first's inbound traffic.
        external_id=(from_number or messaging_service_sid)[:200],
    )
    sms.store_credentials(
        connection,
        sid=account_sid,
        token=token,
        from_number=from_number,
        messaging_service_sid=messaging_service_sid,
    )
    # Not used by Twilio, which authenticates its callbacks with a signature over
    # the auth token rather than a shared secret — but minted anyway so the
    # connection is not the one row in the table with an empty digest, and so a
    # rotation from the generic settings page is harmless rather than an error.
    connection.rotate_webhook_secret()

    try:
        # The savepoint is not optional: an IntegrityError marks the surrounding
        # transaction unusable, so without it a duplicate number would poison
        # every query for the rest of the request rather than becoming the
        # message below. ``views.connection_create`` wraps its insert for the
        # same reason.
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        # Can be another workspace's row. The wording never says which
        # (SECURITY-BASELINE §1).
        return DUPLICATE_ACCOUNT_ERROR

    messages.success(
        request,
        f"Connected {connection.display_name}. Paste the webhook URL below into Twilio to start receiving messages.",
    )
    return ""


@login_required
@require_permission("manage_channels")
@require_GET
def sms_settings(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """SPEC §6.6's configurable copy, the cost hint and the A2P checklist."""
    return render(request, "channels/sms_settings.html", _settings_context(request, workspace_id))


@login_required
@require_permission("manage_channels")
@require_POST
def sms_settings_update(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Save the settings. Blank fields mean "use the default", not "send nothing".

    That is the reason every field is optional and none is validated for
    presence: the three replies below are legally required, so a workspace that
    empties them gets the shipped wording rather than silence
    (``SmsSettings.help_reply`` and its siblings decide that, not this view).
    """
    row = _settings_row(request)
    row.help_text_body = (request.POST.get("help_text_body") or "").strip()[:MAX_SETTINGS_CHARS]
    row.opt_out_confirmation = (request.POST.get("opt_out_confirmation") or "").strip()[:MAX_SETTINGS_CHARS]
    row.opt_in_confirmation = (request.POST.get("opt_in_confirmation") or "").strip()[:MAX_SETTINGS_CHARS]
    row.per_segment_cost = _cost(request.POST.get("per_segment_cost"))
    row.a2p_brand_registered = bool(request.POST.get("a2p_brand_registered"))
    row.a2p_campaign_approved = bool(request.POST.get("a2p_campaign_approved"))
    row.save()
    messages.success(request, "SMS settings saved.")
    return redirect(reverse("channels:sms_settings", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("edit_flows")
@require_POST
def sms_segment_preview(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Count the segments in a draft message and render the hint fragment.

    ``edit_flows`` rather than ``manage_channels``: the callers are the
    ``send_sms`` node's config panel and L6-B's broadcast composer, and the
    person typing is an author who may well not administer channels. It reads
    one settings row and changes nothing.

    The arithmetic is :func:`apps.channels.segments.segments_for`, which is pure
    and shared, so the number this endpoint renders and the number a test asserts
    are the same number rather than two implementations agreeing by luck.
    """
    text = (request.POST.get("text") or "")[:MAX_PREVIEW_CHARS]
    count = segments.segments_for(text)
    cost = _settings_row(request).per_segment_cost
    return render(
        request,
        "channels/_sms_segments.html",
        {
            "count": count,
            # None when nobody has entered a price. The template says nothing
            # about cost in that case rather than showing a confident zero.
            "estimated_cost": cost * count.segments if cost is not None else None,
        },
    )


def _settings_context(request: WorkspaceRequest, workspace_id: str) -> dict[str, Any]:
    from apps.channels.models import DEFAULT_HELP_TEXT, DEFAULT_OPT_IN_TEXT, DEFAULT_OPT_OUT_TEXT

    return {
        "settings_row": _settings_row(request),
        "default_help_text": DEFAULT_HELP_TEXT,
        "default_opt_out_text": DEFAULT_OPT_OUT_TEXT,
        "default_opt_in_text": DEFAULT_OPT_IN_TEXT,
        "keywords": {
            "opt_out": sorted(sms.OPT_OUT_KEYWORDS),
            "help": sorted(sms.HELP_KEYWORDS),
            "opt_in": sorted(sms.OPT_IN_KEYWORDS),
        },
        "update_url": reverse("channels:sms_settings_update", kwargs={"workspace_id": workspace_id}),
        "preview_url": reverse("channels:sms_segment_preview", kwargs={"workspace_id": workspace_id}),
        "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
    }


def _settings_row(request: WorkspaceRequest) -> SmsSettings:
    """This workspace's settings row, unsaved when it has never been saved.

    Unsaved rather than ``get_or_create``: a GET must not write, and the three
    reply properties fall back to the defaults on their own, so an empty
    instance is already a complete answer. The POST view saves whatever comes
    back, which is the only place a row is created.
    """
    row = SmsSettings.objects.for_workspace(request.workspace).first()
    return row or SmsSettings(workspace=request.workspace)


def _cost(raw: Any) -> Decimal | None:
    """A per-segment price, or None for "not set".

    None rather than zero for an unparseable value: a blank field and a typo
    both mean "we do not know what this costs", and rendering an estimate of
    0.00 would be a confident wrong answer. Negative prices are refused for the
    same reason.
    """
    text = (raw or "").strip() if isinstance(raw, str) else ""
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value < 0 or value >= Decimal("1000"):
        # max_digits=8, decimal_places=5 leaves three digits before the point;
        # a larger value would be refused by the column as a 500 rather than as
        # the "we do not know" this returns.
        return None
    return value.quantize(Decimal("0.00001"))
