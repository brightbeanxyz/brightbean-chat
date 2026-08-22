"""The machine-readable codes an outbound message can carry.

``Message.error`` holds one of these and nothing else. A provider's own error
text routinely quotes the request that caused it — including an access token in
a query string — and this column is rendered in the inbox (SECURITY-BASELINE
§5), so the sentence a human reads is looked up from :data:`REASON_COPY` at
render time rather than stored per row. That is the same "registered copy, not
an f-string at the call site" shape ``apps.notifications.events`` uses.

Three vocabularies, kept apart because they answer different questions:

``Grant``   why a send was allowed — which is not decoration. ``human_agent``
            and ``tag_supplied`` are the difference between a compliant Meta
            send and one that can get a page disabled, and an operator asking
            "why did this go out?" is asking for exactly this word.
``Denial``  why compliance refused. Lands on the message row.
``Failure`` why the provider call did not produce a sent message.
"""

from enum import StrEnum

__all__ = ["Denial", "Failure", "Grant", "REASON_COPY", "describe"]


class Grant(StrEnum):
    """Why :func:`apps.messaging.compliance.can_send` said yes."""

    NO_WINDOW = "no_window"
    IN_WINDOW = "in_window"
    HUMAN_AGENT = "human_agent"
    TAG_SUPPLIED = "tag_supplied"
    TEMPLATE_SUPPLIED = "template_supplied"


class Denial(StrEnum):
    """Why compliance refused. Every one of these ends up on a failed row."""

    OPTED_OUT = "opted_out"
    NO_OPT_IN = "no_opt_in"
    NO_IDENTITY = "no_identity"
    NO_CONNECTION = "no_connection"
    BROADCAST_NOT_ALLOWED = "broadcast_not_allowed"
    OUTSIDE_WINDOW = "outside_window"
    NEEDS_TEMPLATE = "needs_template"
    NEEDS_TAG = "needs_tag"


class Failure(StrEnum):
    """Why the provider call did not produce a sent message."""

    NO_ADAPTER = "no_adapter"
    PROVIDER_REJECTED = "provider_rejected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    RATE_DEFERRED = "rate_deferred"
    RETRIES_EXHAUSTED = "retries_exhausted"
    RETRY_UNSCHEDULABLE = "retry_unschedulable"


#: One sentence per code, for the inbox and the flow-run log. Keyed by the raw
#: string so a stored value from an older release still resolves.
REASON_COPY: dict[str, str] = {
    Grant.NO_WINDOW: "This platform has no messaging window.",
    Grant.IN_WINDOW: "The contact messaged recently, so the window is open.",
    Grant.HUMAN_AGENT: "Sent under the human-agent allowance, available to inbox replies only.",
    Grant.TAG_SUPPLIED: "Sent outside the window under an approved message tag.",
    Grant.TEMPLATE_SUPPLIED: "Sent outside the window using an approved template.",
    Denial.OPTED_OUT: "This contact opted out of messages on this channel.",
    Denial.NO_OPT_IN: "This contact has never given permission to message them on this channel.",
    Denial.NO_IDENTITY: "There is no address for this contact on this channel.",
    Denial.NO_CONNECTION: "This address was captured before a channel connection existed.",
    Denial.BROADCAST_NOT_ALLOWED: "This platform does not permit broadcasts.",
    Denial.OUTSIDE_WINDOW: "The messaging window has closed and this platform offers no way to reopen it.",
    Denial.NEEDS_TEMPLATE: "Outside the messaging window this platform requires an approved template.",
    Denial.NEEDS_TAG: "Outside the messaging window this platform requires an approved message tag.",
    Failure.NO_ADAPTER: "No adapter is installed for this platform.",
    Failure.PROVIDER_REJECTED: "The platform rejected the message.",
    Failure.PROVIDER_UNAVAILABLE: "The platform could not be reached.",
    Failure.RATE_LIMITED: "The platform is throttling this connection.",
    Failure.RATE_DEFERRED: "Waiting for this connection's send rate to allow another message.",
    Failure.RETRIES_EXHAUSTED: "Gave up after retrying this send.",
    Failure.RETRY_UNSCHEDULABLE: "The send failed and another attempt could not be scheduled.",
}


def describe(code: str) -> str:
    """The sentence for ``code``, or the code itself when nothing is registered.

    Falls back rather than raising: this runs on a render path, and a code from
    a newer worker than the web process is not a reason to 500 an inbox.
    """
    if not code:
        return ""
    # A provider code is appended after a colon (``provider_rejected:131047``);
    # the copy belongs to the part before it.
    return REASON_COPY.get(code.split(":", 1)[0], code)
