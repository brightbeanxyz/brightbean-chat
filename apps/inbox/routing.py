"""The inbox's hook at contract 6's ``post_persist`` stage (SPEC §14).

The stage was built for this and shipped empty. ``apps/flows/triggers/stages.py``
says so — "``post_persist`` ships **empty and named**, which is the point of it"
— so everything below is a *registration*. Nothing in ``apps.flows`` changes, and
this module is the only thing in the product that knows inbox rules exist.

Four properties of the stage that this module is written around.

**It runs while automation is paused, and there is no pause check here.**
``RUNS_WHILE_PAUSED`` contains ``post_persist`` structurally, because "labels and
assignment are inbox features and suppressing those during a takeover would break
the takeover". Adding a check would re-implement, wrongly, a decision the router
already made.

**It never consumes the event.** A rule is not a trigger: SPEC §14 evaluates
*every* rule and applies them cumulatively, so there is no "first match wins" and
nothing here may stop the chain. :func:`apply_inbox_rules` returns ``None``,
which ``hooks.HookOutcome`` reads as ``Passed`` for exactly this case. In
particular a rule that marks a thread done does **not** block trigger matching —
and the more surprising half of that is documented on :func:`_mark_done`.

**It runs twice in two different worlds.** Inline it is outside any transaction
of ours and holds no lock, so each write below commits on its own. On the worker
— a replay after any hook at this stage defers — ``queueing.worker.process_action``
has already opened a transaction *and* taken the contact advisory lock, so the
same code runs as one atomic unit inside a savepoint. Both are correct; neither
is assumed.

**Which is why every rule claims before it acts.** ``InboxRuleApplication`` is
the ledger, and its unique constraint is what makes a replay a no-op. Guarding
each action separately instead would be three idempotency stories that can each
be wrong on their own.
"""

import hashlib
import logging
from typing import Any

from django.db import IntegrityError, transaction

from apps.inbox.rules import RULE_EVENTS, RuleInput, compile_rule, matches

__all__ = ["apply_inbox_rules", "event_ref_for", "register_inbox_hooks"]

logger = logging.getLogger(__name__)

#: This hook's name in the registry. Unique across every stage (``register_hook``
#: enforces that), which is what lets ``unregister_hook("inbox_rules")`` in a
#: test need nothing else.
HOOK_NAME = "inbox_rules"


def register_inbox_hooks() -> None:
    """Called from ``InboxConfig.ready()``. Idempotent."""
    from apps.flows.triggers.hooks import Stage, register_hook

    register_hook(apply_inbox_rules, stage=Stage.POST_PERSIST, name=HOOK_NAME, replace_existing=True)


def event_ref_for(event: Any) -> str:
    """A bounded, stable name for one inbound event.

    Hashed rather than stored raw, for the reasons
    :func:`apps.flows.triggers.handlers.route_idempotency_key` gives about the
    same value: the provider's id is attacker-controlled and unbounded, so it is
    both a size problem and a way for two long ids sharing a prefix to collide.
    ``(connection, provider_event_id)`` is already unique upstream in
    ``webhook_event_log``, so the digest identifies the event exactly.
    """
    raw = getattr(event, "provider_event_id", "") or ""
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def apply_inbox_rules(context: Any) -> None:
    """Evaluate this workspace's inbox rules against one inbound message.

    Returns ``None`` in every path, including the ones that did work: a hook that
    "only *does* something — L6-C applying a label" is the case ``hooks.py``
    documents ``None`` for. It never returns ``Deferred`` either, and that is
    load-bearing rather than incidental — ``pipeline._route`` hands the **whole
    stage** to the worker when any hook defers, so deferring here would be this
    module arranging its own re-entry.

    Nothing escapes it. The registry would log a raise and treat it as a pass
    anyway ("one broken inbox rule must not cost the reply"), but that is the
    stage's backstop, not this function's contract: a rule whose *action* fails
    must not cost the other actions in the same rule, let alone the other rules.
    """
    from apps.inbox.models import InboxRule

    if str(getattr(context.event, "type", "")) not in RULE_EVENTS:
        return
    if context.contact is None or context.conversation is None:
        # A comment has neither: ``apps.messaging.ingest`` deliberately creates
        # no contact for one, so there is no thread to label and nobody to
        # assign it to.
        return
    event_ref = event_ref_for(context.event)
    if not event_ref:
        # Nothing to key the ledger on, so a replay could not be told from a new
        # event. Every adapter synthesises an id when its platform does not give
        # one (``apps.channels.ingest.synthetic_event_id``), so this is a
        # backstop rather than a path.
        logger.warning("Skipping inbox rules for an event with no provider id on connection %s", context.connection.pk)
        return

    workspace_id = context.connection.workspace_id
    candidates = list(
        InboxRule.objects.for_workspace(workspace_id).filter(enabled=True).order_by("priority", "name")
    )
    if not candidates:
        return

    rule_input = RuleInput.from_event(context)
    for rule in candidates:
        try:
            if not matches(compile_rule(rule), rule_input):
                continue
        except Exception:
            # A stored condition the engine cannot read — a segment that was
            # deleted, a custom field that changed type. One unreadable rule
            # must not stop the readable ones behind it.
            logger.exception("Inbox rule %s could not be evaluated", rule.pk)
            continue
        if not _claim(context.conversation, rule, event_ref):
            continue
        _apply(context, rule)


def _claim(conversation: Any, rule: Any, event_ref: str) -> bool:
    """Record that this rule is acting on this event. False when it already has.

    In its own ``atomic()`` block whatever the caller is inside. On the worker
    path this is a savepoint, and catching the ``IntegrityError`` without one
    would poison the transaction that is holding the contact advisory lock —
    which is the same reasoning ``hooks._run_one`` applies one level up.
    """
    from apps.inbox.models import InboxRuleApplication

    try:
        with transaction.atomic():
            InboxRuleApplication(conversation=conversation, rule=rule, event_ref=event_ref).save()
    except IntegrityError:
        return False
    return True


def _apply(context: Any, rule: Any) -> None:
    """Run one matched rule's actions, each guarded on its own.

    Grouped by verb rather than walked in order, for one reason each: several
    ``add_label`` actions are one insert; two assignees would be a rule whose
    outcome depended on which ran last (``rules.validate_actions`` refuses them,
    and this reads the first regardless, so a hand-edited row cannot surprise
    anyone); and ``mark_done`` is a flag.

    An action that fails is logged and the others still run. The rule stays
    claimed either way: a replay re-running the two actions that worked, to
    retry the one that cannot, is a worse trade than an entry in the log.
    """
    actions = rule.actions_json if isinstance(rule.actions_json, list) else []
    verbs = [item for item in actions if isinstance(item, dict)]

    label_ids = [str(item.get("label_id") or "") for item in verbs if item.get("type") == "add_label"]
    assignee_id = next(
        (str(item.get("user_id") or "") for item in verbs if item.get("type") == "assign_to_member"), ""
    )
    close = any(item.get("type") == "mark_done" for item in verbs)

    if label_ids:
        _guarded(rule, "add_label", lambda: _add_labels(context.conversation, label_ids))
    if assignee_id:
        _guarded(rule, "assign_to_member", lambda: _assign(context.conversation, assignee_id))
    if close:
        _guarded(rule, "mark_done", lambda: _mark_done(context.conversation))


def _guarded(rule: Any, verb: str, work: Any) -> None:
    try:
        with transaction.atomic():
            work()
    except Exception:
        logger.exception("Inbox rule %s could not apply %s", rule.pk, verb)


def _add_labels(conversation: Any, label_ids: list[str]) -> None:
    """Attach every label the rule names, in one insert.

    ``ignore_conflicts`` rather than a read-then-write: the unique constraint is
    already the arbiter for two events arriving together, and a select first
    would only widen the window it closes.
    """
    from apps.inbox.models import MAX_LABELS_PER_CONVERSATION, ConversationLabel, ConversationLabelLink

    labels = list(ConversationLabel.objects.for_workspace(conversation.workspace_id).filter(pk__in=label_ids))
    if not labels:
        # Every id was checked against this workspace when the rule was saved,
        # so an empty result means the label has since been deleted. Not an
        # error: the rule simply has nothing left to apply.
        return
    existing = ConversationLabelLink.objects.for_workspace(conversation.workspace_id).filter(
        conversation=conversation
    )
    room = MAX_LABELS_PER_CONVERSATION - existing.count()
    if room <= 0:
        return
    ConversationLabelLink.objects.bulk_create(
        [ConversationLabelLink.unsaved(conversation=conversation, label=label) for label in labels[:room]],
        ignore_conflicts=True,
    )


def _assign(conversation: Any, user_id: str) -> None:
    """Assign the thread — **only when nobody has it**.

    This stage runs during an agent takeover (``RUNS_WHILE_PAUSED``), so the
    unguarded version has a failure mode with real teeth: an agent claims a
    thread, the contact replies, and the rule hands it straight back to whoever
    the rule names. Assigning only an unassigned thread is both what a helpdesk
    wants and what makes the action idempotent under replay.

    Through ``apps.messaging.services`` and nowhere else — ROADMAP contract 1,
    and ``apps/messaging/tests/test_write_sites.py`` scans for the alternative.
    """
    from apps.members.models import WorkspaceMembership
    from apps.messaging import services as messaging

    fresh = _reload(conversation)
    if fresh is None or fresh.assignee_id is not None:
        return
    # WorkspaceMembership is not workspace-scoped, so this is a plain filter on
    # the column — the shape apps/inbox/views.py::_membership uses. Re-checked
    # here as well as at save time because a member can leave a workspace
    # between the two.
    membership = (
        WorkspaceMembership.objects.filter(workspace_id=conversation.workspace_id, user_id=user_id)
        .select_related("user")
        .first()
    )
    if membership is None:
        return
    messaging.assign_conversation(fresh, membership.user)


def _mark_done(conversation: Any) -> None:
    """Close the thread, if it is open.

    **A rule marking a thread done does not block trigger matching**, and the
    reason is structural rather than a promise: this stage returns ``Passed``, so
    ``pipeline._route`` walks on to ``resume`` and ``trigger``, and nothing
    downstream reads ``context.conversation`` — ``is_paused`` was snapshotted
    when the context was built.

    The consequence is worth stating plainly, because it surprises people and
    the rule builder's copy repeats it: **if a trigger then replies, the thread
    reopens.** ``send_outbound`` calls ``open_conversation`` unconditionally and
    that reopens a ``done`` thread, deliberately — "an outbound message on a
    closed conversation is an agent or an automation picking it back up". So
    "mark done" means *done unless something answers*, and a rule that wants the
    other behaviour wants a trigger that does not match.
    """
    from apps.messaging import services as messaging
    from apps.messaging.models import ConversationState

    fresh = _reload(conversation)
    if fresh is None or fresh.state != ConversationState.OPEN:
        return
    messaging.close_conversation(fresh)


def _reload(conversation: Any) -> Any:
    """The thread as it is now, scoped.

    ``context.conversation`` was read before the stage chain started, so its
    ``state`` and ``assignee_id`` are a snapshot — and both guards above are
    about what is true *at the moment of writing*. One indexed read per action
    that needs it, and only for the rules that matched.
    """
    from apps.messaging.models import Conversation

    return (
        Conversation.objects.for_workspace(conversation.workspace_id).filter(pk=conversation.pk).first()
    )
