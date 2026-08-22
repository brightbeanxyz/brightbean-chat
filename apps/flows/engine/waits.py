"""SPEC §9.3: what a paused execution is waiting for, and what wakes it.

    Wait types stored in wait_config:
    - quick_reply / buttons: map of expected ids -> handles, optional followup
      timer (schedule followup_timer action; on fire, follow ``timeout``
      handle), optional retry-on-unmatched (max 5, counter in wait_config).
    - data_collection: field target, reply type validation (text, email, phone,
      number, date, url), retry limit (default 3), timeout.
    - smart_delay: resumed only by its scheduled_action.

``wait_config`` is a ``jsonb`` column, so the shapes are plain dicts and this
module is where they are written and read. Two builders and one reader, rather
than three nodes each knowing the layout.

**The subtle half is §9.3's routing rule**, which :func:`attempt_resume`
implements:

    unmatched input with no retry -> execution keeps waiting AND the event falls
    through to (3) trigger matching, so keywords still work mid-flow only if
    nothing consumed the event; matched or retried input is consumed.

So "not consumed" and "no longer waiting" are *different things*, and conflating
them is the bug this function exists to avoid. A contact parked on a question
who types ``STOP`` must still reach the keyword trigger, and must still be
parked on the question afterwards if the keyword did nothing. That is why the
return type is a two-way verdict about the **event** and never about the
execution: L4-A reads it to decide whether to keep walking its hook chain.

Precedence — which waiting execution is offered the event, and whether a paused
conversation is offered one at all — is L4-A's (contract 3: this layer only
reads ``automation_paused_until``). This function answers one question about one
execution.

**Every wait carries a token.** The runner mints one
(:func:`apps.flows.engine.runner._tokenised`); every scheduled wake-up carries
it; a resume whose token no longer matches does nothing. That is what makes a
followup timer harmless after the contact has already replied.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.utils import timezone

from apps.flows.engine.graph import Graph
from apps.flows.engine.runner import advance, locked_execution
from apps.flows.engine.sending import deliver, text_message
from apps.flows.models import ExecutionStatus, FlowExecution
from apps.queueing.locks import contact_lock

__all__ = [
    "MAX_RETRY_BUTTONS",
    "MAX_RETRY_DATA_COLLECTION",
    "REPLY_TYPES",
    "WAIT_BUTTONS",
    "WAIT_DATA_COLLECTION",
    "WAIT_SMART_DELAY",
    "Consumed",
    "InvalidAnswerError",
    "NotConsumed",
    "ResumeOutcome",
    "attempt_resume",
    "buttons_wait",
    "data_collection_wait",
    "deadline",
    "normalise_answer",
]

logger = logging.getLogger(__name__)

WAIT_BUTTONS = "buttons"
WAIT_DATA_COLLECTION = "data_collection"
WAIT_SMART_DELAY = "smart_delay"

#: SPEC §11.1 caps retry-on-unmatched at 5; §11.8 caps data-collection retries
#: at 3 and calls 3 the default. The schema enforces both ceilings on the way
#: in; these are the runtime's backstop for a config that predates them.
MAX_RETRY_BUTTONS = 5
MAX_RETRY_DATA_COLLECTION = 3
DEFAULT_RETRY_DATA_COLLECTION = 3

#: SPEC §11.8's reply types.
REPLY_TYPES = ("text", "email", "phone", "number", "date", "url")

_UNITS = {"minutes": "minutes", "hours": "hours", "days": "days"}

#: Longest answer accepted for a free-text question. Inbound text is
#: attacker-controlled (SECURITY-BASELINE §2) and lands in a contact field.
MAX_ANSWER_CHARS = 4096


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Consumed:
    """The event belonged to this execution. L4-A stops here.

    Covers both "the reply matched and the flow moved on" and "the reply did not
    match but was answered with a retry prompt" — §9.3 groups them ("matched or
    retried input is consumed") because in both cases the contact has had a
    response and the event must not also fire a keyword trigger.
    """

    execution: FlowExecution
    reason: str = ""


@dataclass(frozen=True)
class NotConsumed:
    """The event was not for this execution. L4-A keeps going.

    Says nothing about whether the execution is still waiting — usually it is.
    """

    reason: str = ""


type ResumeOutcome = Consumed | NotConsumed


class InvalidAnswerError(ValueError):
    """A data-collection answer did not match its declared reply type."""


# ---------------------------------------------------------------------------
# Building a wait
# ---------------------------------------------------------------------------


def deadline(config: Any, *, handle: str = "timeout", now: datetime | None = None) -> dict[str, Any] | None:
    """Turn a ``{enabled, delay, unit}`` block into a wait's ``timeout`` entry.

    Shared by SPEC §11.1's ``followup`` and §11.8's ``timeout``: the same three
    keys, the same meaning, and the runner arms the queue row from whatever this
    returns. ``None`` means the wait has no deadline and simply waits.
    """
    if not isinstance(config, dict) or not config.get("enabled"):
        return None
    delay = config.get("delay")
    unit = _UNITS.get(str(config.get("unit") or ""))
    if not isinstance(delay, int) or delay <= 0 or unit is None:
        # Enabled with nothing to count: the builder allows it (only `enabled`
        # is required), and a wait that silently never times out is a better
        # answer than one that fires immediately.
        logger.warning("A timeout block is enabled but carries no usable delay: %r", config)
        return None
    run_at = (now or timezone.now()) + timedelta(**{unit: delay})
    return {"handle": handle, "run_at": run_at.isoformat()}


def buttons_wait(
    node_id: str,
    *,
    buttons: list[dict[str, Any]] | None = None,
    quick_replies: list[dict[str, Any]] | None = None,
    followup: Any = None,
    retry_unmatched: Any = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """SPEC §9.3's "map of expected ids -> handles", plus its two options.

    ``labels`` maps a case-folded reply *label* to the id it stands for, and it
    exists because of how quick replies actually come back. Telegram's reply
    keyboards return an ordinary text message whose body is the button's label,
    not a callback id (SPEC §6.2), so matching on ids alone would leave every
    Telegram quick reply unmatched. The caller supplies the rendered labels, so
    a label carrying a ``{{placeholder}}`` matches what the contact was shown.
    """
    handles: dict[str, str] = {}
    for button in buttons or []:
        # A URL button opens a link; it never comes back as an event, so it gets
        # no entry here even though the graph exposes a `btn:<id>` handle for it.
        if isinstance(button, dict) and isinstance(button.get("id"), str) and button.get("action") != "url":
            handles[button["id"]] = f"btn:{button['id']}"
    for reply in quick_replies or []:
        if isinstance(reply, dict) and isinstance(reply.get("id"), str):
            handles[reply["id"]] = f"qr:{reply['id']}"

    config: dict[str, Any] = {
        "type": WAIT_BUTTONS,
        "node_id": node_id,
        "handles": handles,
        "labels": {key.casefold(): value for key, value in (labels or {}).items()},
    }
    retry = _retry_block(retry_unmatched, cap=MAX_RETRY_BUTTONS, text_key="text")
    if retry is not None:
        config["retry"] = retry
    timeout = deadline(followup)
    if timeout is not None:
        config["timeout"] = timeout
    return config


def data_collection_wait(
    node_id: str,
    *,
    reply_type: str,
    target: dict[str, Any],
    retry: Any = None,
    timeout: Any = None,
) -> dict[str, Any]:
    """SPEC §11.8's wait: what to validate, where to put it, how often to re-ask."""
    config: dict[str, Any] = {
        "type": WAIT_DATA_COLLECTION,
        "node_id": node_id,
        "reply_type": reply_type,
        "target": dict(target),
        "retry": _retry_block(retry, cap=MAX_RETRY_DATA_COLLECTION, text_key="invalid_text")
        or {"max": DEFAULT_RETRY_DATA_COLLECTION, "count": 0, "text": ""},
    }
    parsed = deadline(timeout)
    if parsed is not None:
        config["timeout"] = parsed
    return config


def _retry_block(raw: Any, *, cap: int, text_key: str) -> dict[str, Any] | None:
    """Normalise the two differently-spelled retry blocks into one shape.

    §11.1 writes ``retry_unmatched {enabled, max, text}`` and §11.8 writes
    ``retry {max, invalid_text}``. Storing both as ``{max, count, text}`` means
    :func:`attempt_resume` has one counter to increment rather than two spellings
    to remember.
    """
    if not isinstance(raw, dict):
        return None
    if "enabled" in raw and not raw.get("enabled"):
        return None
    limit = raw.get("max")
    if not isinstance(limit, int) or limit <= 0:
        limit = cap
    return {"max": min(limit, cap), "count": 0, "text": str(raw.get(text_key) or "")}


# ---------------------------------------------------------------------------
# Resuming from an inbound event
# ---------------------------------------------------------------------------


def attempt_resume(execution: FlowExecution, event: Any) -> ResumeOutcome:
    """SPEC §9.3, for one execution and one normalized event.

    ``event`` is an :class:`apps.channels.events.NormalizedEvent`. L4-A calls
    this after deciding *which* execution the event might belong to; the
    precedence rules and the automation-pause check are its, not this
    function's.

    Takes the contact lock itself. An inbound event and a followup timer racing
    for the same execution is the ordinary case, not an edge case, and whichever
    arrives second has to see the first one's writes.
    """
    with transaction.atomic(), contact_lock(execution.contact_id):
        current = locked_execution(execution)
        if current is None or current.status != ExecutionStatus.WAITING_REPLY:
            return NotConsumed("not waiting for a reply")

        if not _same_channel(current, event):
            # SPEC §9.3 routes to the "waiting execution on that channel". An
            # execution parked on Telegram must not eat an Instagram DM.
            return NotConsumed("waiting on a different channel")

        config = current.wait_config if isinstance(current.wait_config, dict) else {}
        kind = config.get("type")
        if kind == WAIT_BUTTONS:
            return _resume_buttons(current, event, config)
        if kind == WAIT_DATA_COLLECTION:
            return _resume_data_collection(current, event, config)
        # smart_delay, and anything a later layer parks on that this does not
        # know: "resumed only by its scheduled_action" (SPEC §9.3).
        return NotConsumed(f"wait type {kind!r} is not resumed by inbound events")


def _resume_buttons(execution: FlowExecution, event: Any, config: dict[str, Any]) -> ResumeOutcome:
    handles = _mapping(config.get("handles"))
    labels = _mapping(config.get("labels"))
    payload = getattr(event, "payload", None)

    chosen = _match_choice(payload, handles, labels)
    if chosen is not None:
        logger.debug("Execution %s: reply matched %s", execution.pk, chosen)
        moved = advance(execution, Graph(execution.flow_version.graph_json), chosen)
        return Consumed(moved, reason="matched")

    return _retry_or_fall_through(execution, config, reason="unmatched")


def _mapping(raw: Any) -> dict[str, Any]:
    """A stored wait map, or an empty one. ``wait_config`` is untyped jsonb."""
    return raw if isinstance(raw, dict) else {}


def _match_choice(payload: Any, handles: dict[str, Any], labels: dict[str, Any]) -> str | None:
    """The handle this reply chose, or ``None``.

    Two ways in, in order of reliability: the id a platform sends back for a
    button press, then the label text a platform that has no ids sends instead
    (SPEC §6.2's Telegram reply keyboards). Text matching is trimmed and
    case-folded because a contact typing "yes" chose the "Yes" button.
    """
    button_id = str(getattr(payload, "button_id", "") or "")
    if button_id and button_id in handles:
        return str(handles[button_id])

    text = str(getattr(payload, "text", "") or "").strip().casefold()
    if text and text in labels:
        matched_id = str(labels[text])
        if matched_id in handles:
            return str(handles[matched_id])
    return None


def _resume_data_collection(execution: FlowExecution, event: Any, config: dict[str, Any]) -> ResumeOutcome:
    payload = getattr(event, "payload", None)
    text = str(getattr(payload, "text", "") or "")
    reply_type = str(config.get("reply_type") or "text")

    try:
        value = normalise_answer(text, reply_type)
    except InvalidAnswerError as exc:
        logger.debug("Execution %s: answer rejected (%s)", execution.pk, exc)
        return _retry_or_fall_through(execution, config, reason="invalid")

    _store_answer(execution, config.get("target"), value, reply_type)
    moved = advance(execution, Graph(execution.flow_version.graph_json), "default")
    return Consumed(moved, reason="answered")


def _retry_or_fall_through(execution: FlowExecution, config: dict[str, Any], *, reason: str) -> ResumeOutcome:
    """SPEC §9.3's fork: re-ask and consume, or keep waiting and let it fall through.

    The second branch is the one worth being precise about. The execution stays
    parked — it is not advanced, not failed, not expired — and the event is
    handed back to L4-A so a keyword trigger still gets a look at it. Only the
    timeout timer moves a wait whose retries are spent.
    """
    retry = config.get("retry") if isinstance(config.get("retry"), dict) else None
    count = int(retry.get("count") or 0) if retry else 0
    limit = int(retry.get("max") or 0) if retry else 0

    if retry is None or count >= limit:
        return NotConsumed(f"{reason}, no retry left")

    retry = {**retry, "count": count + 1}
    execution.wait_config = {**config, "retry": retry}
    execution.save(update_fields=["wait_config", "updated_at"])

    prompt = str(retry.get("text") or "")
    if prompt:
        _send_prompt(execution, prompt, attempt=retry["count"])
    logger.debug("Execution %s: %s, retry %s of %s", execution.pk, reason, retry["count"], limit)
    return Consumed(execution, reason=f"{reason}, retried")


def _send_prompt(execution: FlowExecution, text: str, *, attempt: int) -> None:
    """Re-ask, tolerating a send failure.

    A retry prompt that cannot be delivered must not undo the counter: the
    contact's unmatched reply still happened, and rolling back would let the
    same reply be retried forever.
    """
    from apps.flows.messaging import FacadeUnavailableError
    from apps.flows.rendering import context_for, render

    node_id = str(execution.wait_config.get("node_id") or execution.current_node_id)
    rendered = render(text, context_for(execution.contact, execution.variables))
    try:
        deliver(execution, text_message(rendered), node_id=node_id, attempt=attempt)
    except FacadeUnavailableError:
        logger.warning("Execution %s could not send its retry prompt: messaging is not installed.", execution.pk)


def _same_channel(execution: FlowExecution, event: Any) -> bool:
    """Whether the event arrived on the channel this execution is running on."""
    if execution.channel_connection_id is None:
        return True
    connection = getattr(event, "connection", None)
    return connection is None or getattr(connection, "pk", None) == execution.channel_connection_id


# ---------------------------------------------------------------------------
# SPEC §11.8's validation
# ---------------------------------------------------------------------------


def normalise_answer(text: str, reply_type: str) -> Any:
    """Validate one answer and return the value to store, or raise.

    Returns the *normalised* value rather than the raw text: a phone number
    arrives as "(555) 010-1234" and has to be stored as something an identity
    lookup can match, and a number stored as text would be invisible to every
    numeric operator in SPEC §11.4's table.
    """
    answer = text.strip()
    if not answer:
        raise InvalidAnswerError("an empty reply is not an answer")
    if len(answer) > MAX_ANSWER_CHARS:
        raise InvalidAnswerError("that reply is too long")

    if reply_type == "text":
        return answer
    if reply_type == "email":
        return _email(answer)
    if reply_type == "phone":
        return _phone(answer)
    if reply_type == "number":
        return _number(answer)
    if reply_type == "date":
        return _date(answer)
    if reply_type == "url":
        return _url(answer)
    raise InvalidAnswerError(f"{reply_type!r} is not a reply type")


def _email(answer: str) -> str:
    try:
        validate_email(answer)
    except ValidationError as exc:
        raise InvalidAnswerError("that is not an email address") from exc
    return answer.lower()


def _phone(answer: str) -> str:
    """Strip the punctuation people type and keep an E.164-shaped number.

    Not a full libphonenumber parse — that is a dependency for a validator whose
    job here is "did the contact type a phone number or a sentence". The rule is
    ITU E.164's: an optional ``+``, then 7 to 15 digits.
    """
    digits = "".join(character for character in answer if character.isdigit())
    if not 7 <= len(digits) <= 15:
        raise InvalidAnswerError("that is not a phone number")
    return f"+{digits}" if answer.strip().startswith("+") else digits


def _number(answer: str) -> Decimal:
    try:
        value = Decimal(answer.replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise InvalidAnswerError("that is not a number") from exc
    if not value.is_finite():
        raise InvalidAnswerError("that is not a number")
    return value


def _date(answer: str):  # type: ignore[no-untyped-def]
    from django.utils.dateparse import parse_date

    try:
        # parse_date returns None for "not a date at all" but *raises* for a
        # well-shaped one that does not exist ("2026-13-40"). Both are the same
        # answer to a contact.
        parsed = parse_date(answer)
    except ValueError as exc:
        raise InvalidAnswerError("that is not a date") from exc
    if parsed is None:
        raise InvalidAnswerError("that is not a date")
    return parsed


def _url(answer: str) -> str:
    # http/https only: the value ends up in a link somebody clicks, and a
    # javascript: or data: URL stored on a contact is a stored-XSS ingredient
    # (SECURITY-BASELINE §2).
    try:
        URLValidator(schemes=["http", "https"])(answer)
    except ValidationError as exc:
        raise InvalidAnswerError("that is not a web address") from exc
    return answer


# ---------------------------------------------------------------------------
# Storing the answer
# ---------------------------------------------------------------------------

#: Contact columns a data_collection node may write. Read straight off the
#: condition engine's allowlist minus the two nothing may set: ``created_at`` is
#: the database's and ``last_interaction_at`` is the ingest path's.
WRITABLE_SYSTEM_FIELDS = ("first_name", "last_name", "email", "phone", "locale", "timezone")

#: Which platform an answer's identity belongs to (SPEC §11.8's consent audit).
_IDENTITY_PLATFORM = {"email": "email", "phone": "sms"}


def _store_answer(execution: FlowExecution, target: Any, value: Any, reply_type: str) -> None:
    """Save the answer where the node said, and record consent where §11.8 says.

    A target that no longer resolves is a warning rather than a failure: the run
    has already got its answer, and killing it because somebody deleted a custom
    field last week helps nobody.
    """
    if not isinstance(target, dict):
        logger.warning("Execution %s: data_collection has no target; the answer is discarded.", execution.pk)
        return

    kind = str(target.get("type") or "")
    key = str(target.get("key") or "").strip()
    if kind == "system_field":
        _store_system_field(execution, key, value, reply_type)
    elif kind == "custom_field":
        _store_custom_field(execution, key, value)
    else:
        logger.warning("Execution %s: %r is not a data_collection target type.", execution.pk, kind)


def _store_system_field(execution: FlowExecution, key: str, value: Any, reply_type: str) -> None:
    if key not in WRITABLE_SYSTEM_FIELDS:
        logger.warning("Execution %s: %r is not a writable contact field.", execution.pk, key)
        return
    contact = execution.contact
    setattr(contact, key, str(value))
    contact.save(update_fields=[key, "updated_at"])
    _record_consent(execution, key, str(value), reply_type)


def _store_custom_field(execution: FlowExecution, key: str, value: Any) -> None:
    from apps.contacts.errors import ContactsError
    from apps.contacts.models import CustomField
    from apps.contacts.services import set_field_value

    field = CustomField.objects.for_workspace(execution.workspace_id).filter(name__iexact=key).first()
    if field is None:
        logger.warning("Execution %s: no custom field named %r; the answer is discarded.", execution.pk, key)
        return
    try:
        set_field_value(execution.contact, field, value)
    except ContactsError as exc:
        logger.warning("Execution %s: the answer does not fit field %r: %s", execution.pk, key, exc)


def _record_consent(execution: FlowExecution, key: str, address: str, reply_type: str) -> None:
    """SPEC §11.8's consent audit, through ROADMAP contract 1.

        On valid input: save; if reply_type email/phone also update
        contact.email/phone and create/refresh the corresponding email/SMS
        identity with opt_in true recorded with timestamp + source (consent
        audit).

    The ``source="data_collection"`` string is the record of *why* this
    deployment believes it may message that address, and it is the reason this
    goes through the facade instead of writing an identity row directly.
    """
    from apps.flows.messaging import FacadeUnavailableError, upsert_contact_identity

    platform = _IDENTITY_PLATFORM.get(key) or _IDENTITY_PLATFORM.get(reply_type)
    if platform is None:
        return
    try:
        upsert_contact_identity(execution.contact, platform, address, source="data_collection", opt_in=True)
    except FacadeUnavailableError:
        logger.warning(
            "Execution %s captured a %s but could not record consent: messaging is not installed.",
            execution.pk,
            platform,
        )
