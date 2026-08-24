"""``POST /api/v1/messages`` — SPEC §17's send endpoint.

One route, and almost all of it is translation. The send itself is
``apps.messaging.services.send_via_api``, which L3-A shipped for this endpoint:
*"A send from the public API (#25). Automation rules, no agent allowance."*
That last clause matters — the human-agent allowance is hard-coded to inbox
sends (SPEC §22), and an API caller must not be able to buy a seven-day Meta
window by claiming to be a person.

**Compliance never raises.** ROADMAP contract 1 makes a denial a value: the
facade returns a ``Message`` with ``status=failed`` and a code from
``apps.messaging.codes.Denial``. This router is where that becomes SPEC §17's
422 with a machine-readable reason, and the distinction it draws is the one that
matters to a caller — a *denial* means "this send is not allowed and retrying
will not help", while a *failure* means "the platform did not take it this
time", which the send pipeline is already retrying.

**One ``Failure`` code breaks that second half of the dichotomy on purpose:**
``withdrawn`` (``apps.messaging.services.withdraw_send``) means work that
produced this message was cancelled — nothing is retrying it, ever, same as a
denial. So it gets the same treatment as a ``Denial`` here even though it
lives in the other enum; see ``_TERMINAL_CODES`` below.
"""

import uuid
from typing import Any

from ninja import Router, Status

from apps.api.errors import ApiError
from apps.api.requests import ApiRequest
from apps.api.schemas import MessageOut, MessageSend
from apps.api.serializers import message_payload
from apps.channels.events import OutboundMessage, TextBlock
from apps.common.shortcuts import get_scoped_object_or_404
from apps.members.decorators import require_permission
from apps.messaging.codes import Denial, Failure, describe
from apps.messaging.models import MessageStatus

router = Router(tags=["messages"])

#: How long an idempotency key a caller may supply. Long enough for a UUID or a
#: provider's own event id, short enough that the column is not a payload.
MAX_IDEMPOTENCY_KEY_CHARS = 128

#: Codes that get SPEC §17's 422 rather than the 201 an ordinary, still-retrying
#: failure gets — every ``Denial`` (compliance refused this outright), plus
#: ``Failure.WITHDRAWN`` (cancelled work, not a compliance question, but just as
#: permanently not going to send). Keyed to a response ``code`` rather than one
#: shared label, so a caller can tell "compliance said no" from "somebody
#: cancelled this" without parsing the human sentence.
_TERMINAL_CODES: dict[str, str] = {
    **{code.value: "compliance_denied" for code in Denial},
    Failure.WITHDRAWN.value: "withdrawn",
}


@router.post("/messages", response={201: MessageOut}, url_name="messages_send")
@require_permission("reply_in_inbox")
def send_message(request: ApiRequest, payload: MessageSend) -> Status[dict[str, Any]]:
    """Send one message to a contact on one connection, with ``source="api"``.

    ``idempotency_key`` is the caller's to choose and is the whole retry story
    (SPEC §9.4): send the same key twice and the second call returns the first
    message rather than sending again. Omitting it generates one, which means
    the call is *not* idempotent — documented, and the reason the field exists.
    """
    from apps.channels.models import ChannelConnection
    from apps.contacts.models import Contact, ContactStatus
    from apps.messaging.services import send_via_api

    contact = get_scoped_object_or_404(Contact, request.workspace, pk=payload.contact_id)
    if contact.status != ContactStatus.ACTIVE:
        # A deleted contact is invisible from outside; compliance would refuse
        # this anyway, but a 404 is the answer every other route gives.
        raise ApiError("No such contact.", code="not_found", status=404)
    connection = get_scoped_object_or_404(ChannelConnection, request.workspace, pk=payload.connection_id)

    idempotency_key = (payload.idempotency_key or "").strip() or f"api:{uuid.uuid4()}"
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise ApiError(
            f"idempotency_key must be at most {MAX_IDEMPOTENCY_KEY_CHARS} characters.",
            code="invalid_request",
            status=422,
        )

    outbound = OutboundMessage(blocks=(TextBlock(text=payload.body.text),))
    message = send_via_api(
        workspace=request.workspace,
        contact=contact,
        connection=connection,
        outbound=outbound,
        idempotency_key=idempotency_key,
    )

    reason = (message.error or "").split(":", 1)[0]
    if message.status == MessageStatus.FAILED and reason in _TERMINAL_CODES:
        raise ApiError(
            describe(message.error),
            code=_TERMINAL_CODES[reason],
            status=422,
            reason=reason,
            message_id=str(message.pk),
        )

    return Status(201, message_payload(message))
