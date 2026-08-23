"""SPEC §6.6's carrier compliance, as one hook at contract 6's ``hard_optout`` stage.

    Mandatory carrier compliance handled in-core, not in flows: inbound
    STOP/UNSUBSCRIBE/CANCEL/QUIT/END sets opted_out_at, sends the confirmation
    reply, and blocks all future sends to that identity. HELP returns
    configurable help text. START/UNSTOP re-opts in. These are hard-coded before
    trigger matching.

"Before trigger matching" is the whole requirement, and it is why this is a
registration rather than an edit. ``apps.flows.triggers.hooks`` runs
``hard_optout`` first of five stages and stops the chain when a hook consumes,
so a keyword handled here never reaches the matcher — and no flow, however it is
drawn, can intercept a STOP before this runs or start a flow after it. SPEC §19
is explicit that opt-out lives at a chokepoint "so it cannot be bypassed"; this
is the inbound half of that, the way ``apps.messaging.compliance`` is the
outbound half.

--------------------------------------------------------------------------
What this file does not do
--------------------------------------------------------------------------

It does **not** write ``identity.opted_out_at``. ROADMAP contract 3 gives that
column one write site, ``apps/messaging/ingest.py``, and an AST scan fails the
build over a second one. The split is:

``apps.channels.providers.sms.parse_events``
    classifies a STOP keyword as ``EventType.OPT_OUT``;
``apps.messaging.ingest``
    applies the column from that event, before routing runs;
here
    sends the confirmation, answers HELP, and re-subscribes on START;
``apps.flows.triggers.stages.opt_out_event``
    consumes the opt-out so nothing downstream acts on it.

That last one is why the opt-out branch below returns ``Passed`` rather than
``Consumed``: the platform-agnostic half already exists, this hook sits in front
of it at a lower priority, and duplicating the consume would mean two places
deciding what an opt-out means.

--------------------------------------------------------------------------
Two failure rules that look wrong until you read the stage's contract
--------------------------------------------------------------------------

**A raising hook at this stage abandons the event.** ``hooks._run_one`` turns it
into ``HookAbortedError`` and the pipeline drops the event entirely — fail
closed, because a stage that cannot establish whether this contact has opted out
must not let anything downstream send to them. That is right for the *check* and
wrong for the *reply*: suppression has already happened in persistence, so a
Twilio outage that stopped a confirmation going out would, with an uncaught
exception, also stop every other SMS in the deployment being routed. So every
send here is wrapped, logged and swallowed.

**A HELP or START whose reply failed is still consumed.** The contact asked a
compliance question; falling through to trigger matching would answer "HELP"
with whatever keyword flow happens to match it, which is worse than answering
nothing.
"""

import hashlib
import logging
from typing import Any

from apps.channels.events import EventType
from apps.channels.models import SmsSettings
from apps.channels.providers.sms import HELP_KEYWORDS, OPT_IN_KEYWORDS, OPT_OUT_KEYWORDS, keyword
from apps.common.platforms import Platform
from apps.flows.triggers.hooks import Consumed, HookOutcome, Passed, Stage, register_hook

logger = logging.getLogger(__name__)

__all__ = ["HOOK_NAME", "HOOK_PRIORITY", "register_sms_hooks", "sms_keywords"]

#: The registered name. Unique across every stage (``register_hook`` says so),
#: and stable, because it is what ``unregister_hook`` and an operator reading
#: ``hook_names()`` use.
HOOK_NAME = "sms_keywords"

#: Lower than the built-ins' 100, so this runs **before**
#: ``stages.opt_out_event`` and can send the confirmation for the event that hook
#: is about to consume. Built-ins sit at 100 precisely so a later stream can go
#: in front of them without renumbering anything.
HOOK_PRIORITY = 50

#: ``Message.idempotency_key`` is 255 characters. A key that did not fit would be
#: refused by the column rather than silently truncated, but a truncated one
#: would be worse: two different events could produce one key and the second
#: contact's confirmation would be skipped as a duplicate.
MAX_KEY_CHARS = 255


def register_sms_hooks() -> None:
    """Called from ``ChannelsConfig.ready()``. Idempotent."""
    register_hook(
        sms_keywords,
        stage=Stage.HARD_OPTOUT,
        name=HOOK_NAME,
        priority=HOOK_PRIORITY,
        replace_existing=True,
    )


def sms_keywords(context: Any) -> HookOutcome:
    """SPEC §6.6's STOP / HELP / START, for an inbound SMS event.

    ``context`` is an :class:`apps.flows.triggers.context.RoutingContext`, typed
    loosely on purpose: this module is imported from ``apps.channels``, which
    ``apps.flows`` sits above, and a typing-only import would still be an import.
    Everything read off it is an attribute the dataclass documents.
    """
    connection = getattr(context, "connection", None)
    if connection is None or connection.platform != Platform.SMS:
        # The cheapest possible check, and first: this hook runs for every
        # inbound event on every platform in the deployment.
        return Passed()

    event = context.event
    if event.type == EventType.OPT_OUT:
        # Persistence has already suppressed this identity. All that is left is
        # to say so — and then to let ``opt_out_event`` consume the event.
        _reply(context, "optout", _settings_for(connection).opt_out_reply)
        return Passed("opt-out confirmed")

    if event.type != EventType.MESSAGE:
        return Passed()

    word = keyword(getattr(event.payload, "text", "") or "")
    if word in OPT_OUT_KEYWORDS:
        # The message half of a STOP. The adapter emits it so the contact's own
        # words land in the thread (``providers.sms._inbound`` explains why),
        # and the opt-out half beside it is what suppresses the identity and
        # carries the confirmation — so there is nothing to *do* here, only
        # something to stop. Consuming it keeps it away from trigger matching,
        # where a keyword trigger on "STOP" would start a flow at somebody who
        # just unsubscribed. ``stages.opt_out_event`` does the same job for the
        # opt-out half; this is the other end of that guarantee.
        return Consumed("sms opt-out")

    if word in HELP_KEYWORDS:
        _reply(context, "help", _settings_for(connection).help_reply)
        return Consumed("sms help")

    if word in OPT_IN_KEYWORDS:
        _resubscribe(context)
        _reply(context, "optin", _settings_for(connection).opt_in_reply)
        return Consumed("sms opt-in")

    return Passed()


def _settings_for(connection: Any) -> SmsSettings:
    """This workspace's SMS copy, or an unsaved row carrying the defaults.

    Unsaved rather than created on demand: the reply has to go out whether or not
    anybody has visited the settings page, and a hook that wrote a row would be a
    write on the inbound path for a workspace that changed nothing.

    A **bare** instance, with no fields set. ``help_reply`` and its two siblings
    are already ``self.<field>.strip() or DEFAULT_…``, so passing the defaults in
    here would put the same fallback in two places — and the one that drifts is
    always the copy.
    """
    try:
        existing = SmsSettings.objects.for_workspace(connection.workspace_id).first()
    except Exception:
        logger.exception("SMS: could not read the settings for workspace %s.", connection.workspace_id)
        existing = None
    return existing or SmsSettings()


def _resubscribe(context: Any) -> None:
    """Restore consent through the facade (SPEC §6.6's START/UNSTOP).

    Imported late and by name: ``apps.messaging`` depends on ``apps.channels``,
    so a module-scope import here would be a cycle — the same call
    ``apps.channels.preview`` makes and for the same reason.

    ``record_opt_in`` is the only door back in, and it delegates to the single
    write site rather than touching ``opted_out_at`` itself. Failure is logged
    and swallowed: raising would abandon the event (see the module docstring),
    and the contact can simply text START again.
    """
    from apps.messaging.models import OptInSource
    from apps.messaging.services import record_opt_in

    identity = getattr(context, "identity", None)
    if identity is None:
        logger.info("SMS: a START arrived on connection %s with no identity to restore.", context.connection.pk)
        return
    try:
        # A typed START *is* an inbound message, which is what the consent audit
        # should record — SPEC §5 fixes the ``opt_in_source`` vocabulary and has
        # no keyword-shaped member.
        record_opt_in(identity, source=OptInSource.MESSAGE_IN)
    except Exception:
        logger.exception("SMS: could not restore consent on connection %s.", context.connection.pk)


def _reply(context: Any, kind: str, text: str) -> None:
    """Send one mandated reply, and never raise.

    Goes through ``services.send_compliance_reply``, which is
    ``send_outbound`` minus the compliance verdict: the message row, the
    idempotency key and the connection's token bucket all still apply, so the
    confirmation appears in the inbox thread and an agent reading it can see
    that the contact was answered.

    Inline, in the webhook request, rather than queued. SPEC §21's acceptance
    criterion is that "STOP suppresses within one inbound event", and a
    confirmation the contact receives a minute later — after the worker next
    runs — reads as though nothing happened and invites a second STOP. The cost
    is one Twilio call inside SPEC §7.1's inline budget, on an event that by
    definition ends the conversation.
    """
    from apps.channels.events import OutboundMessage, TextBlock
    from apps.messaging.services import send_compliance_reply

    contact = getattr(context, "contact", None)
    if contact is None or not text:
        return
    try:
        send_compliance_reply(
            workspace=context.connection.workspace,
            contact=contact,
            connection=context.connection,
            outbound=OutboundMessage(blocks=(TextBlock(text=text),)),
            idempotency_key=_reply_key(kind, context.event),
        )
    except Exception:
        # Logged and swallowed. The suppression itself has already happened in
        # persistence; a confirmation that could not be sent must not take the
        # whole event down with it.
        logger.exception("SMS: could not send the %s reply on connection %s.", kind, context.connection.pk)


def _reply_key(kind: str, event: Any) -> str:
    """SPEC §9.4's idempotency key for a reply that has no execution behind it.

    Keyed on the **inbound event**, so a redelivered STOP produces one
    confirmation rather than two, while a genuine second STOP — a different
    ``MessageSid`` — is answered again, which is what a carrier expects.

    Over-long ids are hashed rather than cut, the rule this codebase applies
    everywhere an identifier meets a bounded column
    (``apps.messaging.identities.bounded_key`` explains it at length): two ids
    agreeing on a long prefix would otherwise become one key and the second
    contact would be answered with silence.
    """
    prefix = f"sms-{kind}:"
    raw = (getattr(event, "provider_event_id", "") or "").replace("\x00", "").strip()
    if not raw:
        # No id to key on. The address plus the second the event arrived is
        # stable across a redelivery of the same batch and distinct between two
        # real messages.
        raw = f"{getattr(event, 'platform_user_id', '')}:{int(event.timestamp.timestamp())}"
    if len(prefix) + len(raw) <= MAX_KEY_CHARS:
        return prefix + raw
    return f"{prefix}sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
