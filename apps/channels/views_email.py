"""Connecting an email channel, and sending a test through it (SPEC §6.7).

``views_telegram._connect`` is the shape this follows, including the ordering
that makes it safe:

1. **Verify before writing.** The credentials are proved against the provider
   before a row exists, so a mistyped SMTP password or a revoked Resend key
   leaves no trace in the connection list. Telegram calls ``getMe``; each
   backend here has its own cheapest authenticated call, in
   ``email_backends.verify_credentials``.
2. **Save inside a savepoint**, so the deployment-wide unique constraint on
   ``(platform, external_id)`` comes back as a message rather than as a request
   whose transaction is already unusable.

There is no step 3. Telegram needs ``setWebhook`` because it holds the secret
itself; an email provider is configured from its own console, so this page
prints the webhook URL and the operator pastes it — which is why the URL is
built from ``settings.APP_URL`` rather than the request, and why the page says
so.

--------------------------------------------------------------------------
external_id is the sending domain
--------------------------------------------------------------------------

SPEC §5 says so, and the unique constraint on ``(platform, external_id)`` is
deployment-wide, so two workspaces cannot both claim one domain. That is the
right answer for email specifically: SPF, DKIM and DMARC are properties of a
domain, one set of DNS records governs it, and two tenants sending as the same
domain would be sharing a reputation neither controls.

--------------------------------------------------------------------------
Credentials
--------------------------------------------------------------------------

All of them go into ``connection.credentials``, which is an encrypted JSON
column, and none of them is ever rendered back — not on a failed submit, not on
the detail page (CONTRIBUTING: "never render a stored secret"). Editing a
connection's credentials means re-entering them, which is the same trade the
Telegram flow makes.
"""

import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.channels.forms import DUPLICATE_ACCOUNT_ERROR
from apps.channels.models import ChannelConnection
from apps.channels.providers import email_backends, email_html
from apps.channels.providers.exceptions import APIError
from apps.channels.unsubscribe import unsubscribe_url
from apps.common.addresses import normalize_email
from apps.common.platforms import Platform
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.members.requests import WorkspaceRequest

logger = logging.getLogger(__name__)

__all__ = ["email_connect", "send_test_email"]

#: Shown when a provider refuses the credentials. One message per provider
#: rather than per failure mode, for the reason ``views_telegram`` gives: an
#: operator's next step is the same either way, and distinguishing "wrong
#: password" from "no such user" is an oracle.
REJECTED_MESSAGES = {
    "smtp": (
        "The mail server did not accept those details. Check the host, port, encryption and "
        "sign-in details with your provider and try again."
    ),
    "resend": "Resend did not accept that API key. Copy it again from the Resend dashboard and try again.",
    "ses": (
        "AWS did not accept those credentials. Check the access key, secret and region, and that the "
        "key is allowed to use SES."
    ),
}

#: What a test send says. Fixed copy, because the point of the test is to prove
#: the transport works, and anything configurable here would be one more thing
#: that could be wrong when it fails.
TEST_SUBJECT = "BrightBean Chat test email"
TEST_BODY = "<p>This is a test from BrightBean Chat.</p><p>If you are reading it, this channel can send email.</p>"


@login_required
@require_permission("manage_channels")
def email_connect(request: WorkspaceRequest, workspace_id: str) -> HttpResponse:
    """Connect an SMTP, Resend or SES sender (SPEC §6.7)."""
    provider = _chosen_provider(request)
    error = ""
    if request.method == "POST":
        error = _connect(request, provider)
        if not error:
            return redirect(reverse("channels:list", kwargs={"workspace_id": workspace_id}))

    return render(
        request,
        "channels/email_connect.html",
        {
            "error": error,
            "provider": provider,
            "providers": email_backends.PROVIDERS,
            # Echoed back so a failed submit does not make the operator retype
            # everything. Credentials are deliberately absent from this dict.
            "form": _echoed(request),
            "list_url": reverse("channels:list", kwargs={"workspace_id": workspace_id}),
        },
    )


def _chosen_provider(request: WorkspaceRequest) -> str:
    raw = (request.POST.get("provider") or request.GET.get("provider") or "").strip().lower()
    return raw if raw in email_backends.PROVIDERS else email_backends.DEFAULT_PROVIDER


def _echoed(request: WorkspaceRequest) -> dict[str, str]:
    """The non-secret fields, so a rejected submit is not retyped from scratch.

    Every credential field is absent by construction rather than by filtering:
    this dict names what may be echoed, so adding a secret field to the form
    cannot accidentally add it here too.
    """
    if request.method != "POST":
        return {}
    return {
        name: (request.POST.get(name) or "").strip()
        for name in ("display_name", "from_address", "from_name", "host", "port", "security", "username", "region")
    }


def _connect(request: WorkspaceRequest, provider: str) -> str:
    """Verify, then save. Returns ``""`` on success or the message to show."""
    from_address = normalize_email(request.POST.get("from_address") or "")
    if not from_address:
        return "Enter the address this channel should send from."
    domain = from_address.partition("@")[2]

    credentials = _credentials(request, provider, from_address)
    if isinstance(credentials, str):
        return credentials

    connection = ChannelConnection(
        workspace=request.workspace,
        platform=Platform.EMAIL.value,
        display_name=(request.POST.get("display_name") or "").strip() or domain,
        # SPEC §5: the sending domain. See the module docstring for why this is
        # the right identity for an email channel and not the from-address.
        external_id=domain,
    )
    connection.credentials = credentials  # type: ignore[assignment]

    try:
        # Before any write, so a bad credential leaves nothing behind. The
        # connection object exists only in memory at this point.
        email_backends.verify_credentials(connection)
    except APIError:
        # The provider's own text is deliberately not shown: it quotes the
        # request, which on an auth failure means it can quote the credential
        # (SECURITY-BASELINE §5).
        logger.info("Email connect refused by %s for workspace %s.", provider, request.workspace.pk)
        return REJECTED_MESSAGES.get(provider, REJECTED_MESSAGES["smtp"])

    try:
        with transaction.atomic():
            connection.save()
    except IntegrityError:
        return DUPLICATE_ACCOUNT_ERROR

    messages.success(request, f"Connected {domain}.")
    return ""


def _credentials(request: WorkspaceRequest, provider: str, from_address: str) -> dict[str, Any] | str:
    """The credential dict for this provider, or the message explaining what is missing."""
    common: dict[str, Any] = {
        "provider": provider,
        "from_address": from_address,
        "from_name": (request.POST.get("from_name") or "").strip()[:200],
    }
    if provider == "resend":
        api_key = (request.POST.get("api_key") or "").strip()
        if not api_key:
            return "Paste the API key from your Resend dashboard."
        # Resend's own webhook signing secret. Optional at connect time: bounce
        # handling needs it, sending does not, and an operator who has not
        # created the endpoint yet should not be blocked from connecting.
        return {**common, "api_key": api_key, "signing_secret": (request.POST.get("signing_secret") or "").strip()}
    if provider == "ses":
        key_id = (request.POST.get("access_key_id") or "").strip()
        secret = (request.POST.get("secret_access_key") or "").strip()
        region = (request.POST.get("region") or "").strip().lower()
        if not key_id or not secret or not region:
            return "Enter the access key, the secret and the AWS region."
        return {**common, "access_key_id": key_id, "secret_access_key": secret, "region": region}

    host = (request.POST.get("host") or "").strip()
    if not host:
        return "Enter the SMTP host your provider gave you."
    port = (request.POST.get("port") or "").strip() or "587"
    if not port.isdigit():
        return "The SMTP port is a number — usually 587 for STARTTLS or 465 for SSL."
    security = (request.POST.get("security") or "starttls").strip().lower()
    if security not in {"starttls", "ssl", "none"}:
        return "Choose STARTTLS, SSL or none for the connection encryption."
    return {
        **common,
        "host": host,
        "port": int(port),
        "security": security,
        "username": (request.POST.get("username") or "").strip(),
        "password": request.POST.get("password") or "",
    }


@login_required
@require_permission("manage_channels")
@require_POST
def send_test_email(request: WorkspaceRequest, workspace_id: str, connection_id: str) -> JsonResponse:
    """Send one real email through this connection, to prove it works.

    Answers JSON with **status 200 even for a failure**, the shape
    ``views_telegram.telegram_preview`` established: the request succeeded, and
    what it is reporting is the state of somebody's mail configuration rather
    than an error in this application.

    The recipient is the signed-in member's own address, never one supplied in
    the request. A "send a test to any address" button is an open relay with a
    permission check in front of it, and this deployment's domain reputation is
    what would pay for it.
    """
    connection = get_scoped_object_or_404(
        ChannelConnection,
        request.workspace,
        pk=connection_id,
        platform=Platform.EMAIL.value,
    )
    recipient = normalize_email(getattr(request.user, "email", "") or "")
    if not recipient:
        return JsonResponse({"ok": False, "message": "Your account has no email address to send a test to."})

    envelope = _test_envelope(connection, recipient)
    if not envelope.from_address:
        return JsonResponse({"ok": False, "message": "This connection has no from-address stored."})

    try:
        email_backends.deliver(connection, envelope)
    except APIError:
        logger.info("Test email failed on connection %s.", connection.pk)
        return JsonResponse(
            {
                "ok": False,
                "message": REJECTED_MESSAGES.get(email_backends.provider_for(connection), REJECTED_MESSAGES["smtp"]),
            }
        )
    return JsonResponse({"ok": True, "message": f"Sent a test email to {recipient}."})


def _test_envelope(connection: ChannelConnection, recipient: str) -> email_backends.Envelope:
    """The test message.

    It carries the compliance headers and the footer like any other send, but
    with a link that goes nowhere useful — there is no identity behind a test,
    and minting one would create a consent record for a message nobody consented
    to. The point of including them is that the test proves the *real* shape
    arrives intact: an operator whose provider strips ``List-Unsubscribe`` finds
    out here rather than after a campaign.
    """
    credentials = email_backends.credentials_of(connection)
    from_address = str(credentials.get("from_address") or "")
    html, text = email_html.with_unsubscribe_footer(
        TEST_BODY, email_html.to_plain_text(TEST_BODY), _test_unsubscribe_link()
    )
    return email_backends.Envelope(
        to=recipient,
        subject=TEST_SUBJECT,
        html=html,
        text=text,
        from_address=from_address,
        from_name=str(credentials.get("from_name") or ""),
        headers={
            "List-Unsubscribe": f"<{_test_unsubscribe_link()}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        message_id=email_backends.new_message_id(from_address.partition("@")[2]),
    )


def _test_unsubscribe_link() -> str:
    """A well-formed ``/u/`` URL that resolves to a 404.

    Signed like any other, so the header is structurally valid and a provider
    that validates it accepts the message — and pointing at nothing, because a
    test send has no identity to unsubscribe. Clicking it gets the same bare 404
    every unknown token gets.
    """

    class _NoIdentity:
        pk = "00000000-0000-0000-0000-000000000000"

    return unsubscribe_url(_NoIdentity())
