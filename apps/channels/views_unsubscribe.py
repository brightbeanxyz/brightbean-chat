"""The hosted unsubscribe page (SPEC §6.7, SECURITY-BASELINE §4).

One route, two methods, and the split between them is the whole design.

--------------------------------------------------------------------------
GET shows a page. POST unsubscribes.
--------------------------------------------------------------------------

It is tempting to make the ``GET`` do the work — one click, no page, done. It is
also wrong, and the reason is not CSRF: **link scanners prefetch**. Corporate
mail gateways, Outlook's Safe Links, antivirus plugins and half the webmail
clients in existence fetch every URL in a message before a human sees it. A
destructive ``GET`` would unsubscribe a share of every list on delivery, and the
recipients would never know why they stopped receiving mail.

So ``GET`` renders a confirm page whose only control is a one-button form, and
the click on that button is the click that counts — which is what SPEC §21's
"unsubscribe link suppresses email within one click" is measuring, since the
click on the link in the email is what opened the page.

``POST`` also serves RFC 8058's one-click flow, where the mail client itself
posts ``List-Unsubscribe=One-Click`` with no human ever seeing a page. Both land
here and both do the same thing; the only difference is what comes back, and a
client posting in the background is answered with a bare 200 rather than markup
it will not render.

--------------------------------------------------------------------------
Every failure is the same 404
--------------------------------------------------------------------------

A tampered token, a token minted for another purpose, a token whose identity has
since been deleted: one bare ``Http404`` with no body detail, so a caller learns
nothing about which ids exist (SECURITY-BASELINE §4). Verification is
constant-time inside ``apps.common.signing``.

CSRF is exempt because there is no session: the signed token is the whole
credential, and a mail client posting RFC 8058's body has no cookie to carry.
That is the same position the webhook endpoints take for the same reason.
"""

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.channels.models import SuppressionReason
from apps.channels.suppression import suppress_and_opt_out
from apps.channels.unsubscribe import identity_id_from_token
from apps.common.platforms import Platform

logger = logging.getLogger(__name__)

__all__ = ["unsubscribe"]

#: RFC 8058: the body a mail client posts when the user presses the one-click
#: button the ``List-Unsubscribe-Post`` header advertises.
ONE_CLICK_BODY = "List-Unsubscribe=One-Click"


@csrf_exempt
@require_http_methods(["GET", "POST"])
def unsubscribe(request: HttpRequest, token: str) -> HttpResponse:
    """Confirm on ``GET``, unsubscribe on ``POST``."""
    identity = _identity_or_404(token)

    if request.method == "GET":
        return render(
            request,
            "channels/unsubscribe_confirm.html",
            {"address": identity.platform_user_id, "already": identity.opted_out_at is not None, "done": False},
        )

    already = identity.opted_out_at is not None
    if not already:
        suppress_and_opt_out(
            identity,
            reason=SuppressionReason.UNSUBSCRIBE.value,
            detail="",
            connection=identity.channel_connection,
        )

    if _is_one_click(request):
        # RFC 8058 §3.2: the client wants a 2xx and nothing else. Rendering a
        # page here would be markup nobody looks at, and some clients treat a
        # large body as a failure.
        return HttpResponse(status=200)
    return render(
        request,
        "channels/unsubscribe_done.html",
        {"address": identity.platform_user_id, "already": already, "done": True},
    )


def _identity_or_404(token: str) -> Any:
    """The email identity a token names, or ``Http404``.

    The lookup is a deliberate, greppable ``.unscoped()``: there is no session
    and therefore no workspace on this path, and the signed token is what
    authorises the read (CONTRIBUTING). It is the only unscoped query in this
    module.

    Narrowed to email identities so a token cannot be pointed at an identity on
    another platform even in the impossible case that one shares an id — the
    unsubscribe page speaks about a mailbox, and opting a Telegram chat out from
    here would be a surprise with a compliance record attached.
    """
    from apps.messaging.models import ContactChannelIdentity

    identity_id = identity_id_from_token(token)
    if not identity_id:
        raise Http404
    try:
        identity = (
            ContactChannelIdentity.objects.unscoped()
            .select_related("workspace", "channel_connection")
            .filter(pk=identity_id, platform=Platform.EMAIL.value)
            .first()
        )
    except (ValidationError, ValueError, TypeError):
        # A signed token whose payload is not a UUID. Only reachable with our
        # own signing key, so this is a bug rather than an attack — but it is on
        # an unauthenticated route, and a 500 there is a denial-of-service
        # primitive.
        #
        # ``ValidationError`` first and by name: that is what a UUIDField raises
        # for a malformed value, and catching only the ValueError underneath it
        # let this path 500. The same three, in the same order, as
        # ``apps.contacts.imports._locked_run``.
        raise Http404 from None
    if identity is None:
        raise Http404
    return identity


def _is_one_click(request: HttpRequest) -> bool:
    """Whether this POST is a mail client's RFC 8058 one-click, not a person.

    Read from the form body rather than from a header, because that is what the
    RFC specifies and because a header is not something the page's own form
    could accidentally match.
    """
    return request.POST.get("List-Unsubscribe") == "One-Click"
