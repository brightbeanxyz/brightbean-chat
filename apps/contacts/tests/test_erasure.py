"""GDPR erasure: that it removes what it claims, and nothing else (issue #29).

Two assertions carry this module, and they are opposites.

*Removed what it claims* is easy to write badly — a list of `assert not
X.objects.filter(...).exists()` grows only when somebody remembers to grow it,
and the model it forgets is the one that leaks. So the sweep here walks the
**model graph** instead: every reference to ``Contact`` anywhere in the project,
classified, with a test that fails when a new one appears unclassified. That is
the mechanism ``apps/messaging/tests/test_write_sites.py`` and
``tests/idor.py``'s ``WAIVED_ROUTES`` already use, and it is what turns "we
thought of everything" into a build failure when we did not.

*And nothing else* needs a control group. Every fixture below exists twice — a
victim and a bystander in the same workspace, plus a whole second tenancy — and
a nonce string is threaded through every free-text field so a single query can
ask "does this text survive anywhere in the database".
"""

import uuid
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.db import IntegrityError
from django.utils import timezone

from apps.broadcasts.models import Broadcast, BroadcastRecipient, BroadcastStatus, RecipientStatus
from apps.campaigns.models import Sequence, SequenceEnrollment
from apps.channels.models import EmailSuppression, SuppressionReason
from apps.common.platforms import Platform
from apps.contacts import erasure
from apps.contacts.models import Contact, ContactErasure, ContactTag, CustomFieldValue, ErasureSource, ErasureStatus
from apps.contacts.services import add_tag, create_contact, create_custom_field, get_or_create_tag, set_field_value
from apps.flows.models import ExecutionStatus, FlowExecution, HandledComment
from apps.flows.tests.support import graph, node, published_flow
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    OptInSource,
)
from apps.messaging.tests.conftest import make_connection
from apps.notifications.models import Notification
from apps.queueing.models import ActionStatus, ActionType, ScheduledAction
from tests.support import Tenancy

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def seed(workspace: Any, *, nonce: str, label: str, user: Any = None) -> dict[str, Any]:
    """One of everything that can name a contact.

    Two distinct strings, and the distinction is what makes
    :class:`TestNoPiiSurvivesAnywhere` mean anything.

    ``nonce`` goes **only** into text that belongs to the contact — their name,
    their address, a custom field value, a message body, a collected variable.
    The sweep then asks the database whether a string only this person ever had
    is still anywhere in it, which is a far better question than a hand-written
    list of columns that goes stale the moment somebody adds one.

    ``label`` names the *workspace's* objects — the tag, the custom field, the
    flow, the sequence, the broadcast, the connection. Those must survive an
    erasure (a tag is vocabulary, not personal data), so putting the nonce in
    them would make the sweep fail for the right reason about the wrong rows.
    It exists only to keep two seeded contacts from colliding on the
    unique-name-per-workspace constraints.
    """
    contact = create_contact(
        workspace,
        first_name=f"Ada{nonce}",
        last_name=f"Lovelace{nonce}",
        email=f"{nonce}@example.test",
        phone="+15550001111",
        source="manual",
    )
    tag, _ = get_or_create_tag(workspace, f"tag-{label}")
    add_tag(contact, tag)
    field = create_custom_field(workspace, name=f"Field {label}", field_type="text")
    set_field_value(contact, field, f"value-{nonce}")

    connection = make_connection(workspace, suffix=label)
    identity = ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.TELEGRAM.value,
        platform_user_id=f"tg-{nonce}",
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source=OptInSource.MESSAGE_IN,
    )
    conversation = Conversation.objects.create(contact=contact, channel_connection=connection)
    inbound = Message(
        conversation=conversation,
        direction=MessageDirection.IN,
        body={"blocks": [{"type": "text", "text": f"inbound {nonce}"}]},
        status=MessageStatus.DELIVERED,
    )
    inbound.save()
    outbound = Message(
        conversation=conversation,
        direction=MessageDirection.OUT,
        source=MessageSource.AUTOMATION,
        body={"blocks": [{"type": "text", "text": f"outbound {nonce}"}]},
        status=MessageStatus.SENT,
    )
    outbound.save()

    noop = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}
    flow = published_flow(workspace, graph([node("a", "action", noop)]), name=f"Flow {label}")
    version = flow.versions.first()
    assert version is not None
    execution = FlowExecution.objects.create(
        contact=contact,
        flow=flow,
        flow_version=version,
        status=ExecutionStatus.WAITING_REPLY,
        variables={"answer": f"typed-{nonce}"},
    )
    comment = HandledComment.objects.create(
        workspace=workspace,
        channel_connection=connection,
        comment_id=f"c-{label}",
        post_id=f"p-{label}",
        commenter_ref=f"ref-{nonce}",
        contact=contact,
        commented_at=timezone.now(),
    )

    sequence = Sequence.objects.create(workspace=workspace, name=f"Seq {label}")
    enrollment = SequenceEnrollment.objects.create(contact=contact, sequence=sequence)

    broadcast = Broadcast.objects.create(
        workspace=workspace,
        channel_connection=connection,
        name=f"Broadcast {label}",
        status=BroadcastStatus.SENT,
        stats={"queued": 1, "sent": 1, "failed": 0, "skipped": 0},
    )
    recipient = BroadcastRecipient.objects.create(
        workspace=workspace,
        broadcast=broadcast,
        contact=contact,
        identity=identity,
        message=outbound,
        status=RecipientStatus.SENT,
    )

    action = ScheduledAction.objects.create(
        workspace=workspace,
        contact_id=contact.pk,
        run_at=timezone.now(),
        type=ActionType.SEND_RETRY,
        payload={"note": f"payload {nonce}"},
        status=ActionStatus.PENDING,
    )
    notification = Notification.objects.create(
        user=user,
        event_type="flow.loop_cap",
        title=f"Reminder: Ada{nonce}",
        body=f"The run for Ada{nonce} stopped",
        payload={"workspace_id": str(workspace.pk), "contact_id": str(contact.pk)},
    )

    EmailSuppression.objects.create(
        workspace=workspace,
        address=f"{nonce}@example.test",
        reason=SuppressionReason.HARD_BOUNCE.value,
    )

    return {
        "contact": contact,
        "tag": tag,
        "field": field,
        "connection": connection,
        "identity": identity,
        "conversation": conversation,
        "inbound": inbound,
        "outbound": outbound,
        "flow": flow,
        "execution": execution,
        "comment": comment,
        "sequence": sequence,
        "enrollment": enrollment,
        "broadcast": broadcast,
        "recipient": recipient,
        "action": action,
        "notification": notification,
    }


@pytest.fixture
def victim(tenancy: Tenancy) -> dict[str, Any]:
    return seed(tenancy.workspace, nonce="zqxvictim", label="v", user=tenancy.owner)


@pytest.fixture
def bystander(tenancy: Tenancy) -> dict[str, Any]:
    """A second contact in the *same* workspace. The control group."""
    return seed(tenancy.workspace, nonce="zqxbystand", label="b", user=tenancy.owner)


def erase(seeded: dict[str, Any], **kwargs: Any) -> ContactErasure:
    return erasure.begin(seeded["contact"], source=ErasureSource.UI, **kwargs)


# ---------------------------------------------------------------------------
# It removes what it claims
# ---------------------------------------------------------------------------


class TestItRemovesWhatItClaims:
    def test_the_contact_row_is_gone(self, victim: dict[str, Any]) -> None:
        erase(victim)

        assert not Contact.objects.unscoped().filter(pk=victim["contact"].pk).exists()

    def test_identities_conversations_and_message_bodies_go(self, victim: dict[str, Any]) -> None:
        erase(victim)

        assert not ContactChannelIdentity.objects.unscoped().filter(pk=victim["identity"].pk).exists()
        assert not Conversation.objects.unscoped().filter(pk=victim["conversation"].pk).exists()
        assert not Message.objects.unscoped().filter(pk__in=[victim["inbound"].pk, victim["outbound"].pk]).exists()

    def test_consent_records_go_with_the_identity(self, victim: dict[str, Any]) -> None:
        """SPEC §11.8's ``opt_in_at`` / ``opt_in_source`` / ``opted_out_at``.

        They are columns on the identity rather than a table of their own, so
        this is the same delete — asserted separately because "the export must
        include consent" and "the erasure must remove it" are the two halves a
        regulator asks about, and only one of them is about the identity row.
        """
        erase(victim)

        assert ContactChannelIdentity.objects.unscoped().filter(opt_in_source=OptInSource.MESSAGE_IN).count() == 0

    def test_tag_links_and_field_values_go(self, victim: dict[str, Any]) -> None:
        erase(victim)

        assert not ContactTag.objects.unscoped().filter(contact_id=victim["contact"].pk).exists()
        assert not CustomFieldValue.objects.unscoped().filter(contact_id=victim["contact"].pk).exists()

    def test_the_m2m_guard_does_not_fire_on_the_cascade(self, victim: dict[str, Any]) -> None:
        """``Contact.tags`` raises ``RuntimeError`` on direct mutation.

        A cascade emits ``pre_delete`` on the through model rather than
        ``pre_clear`` on the relation, so it does not trip — but the erasure
        would be one ``contact.tags.clear()`` away from a 500, and a reviewer
        will ask. This is the answer.
        """
        erase(victim)  # would raise if the receiver fired

        assert not ContactTag.objects.unscoped().filter(contact_id=victim["contact"].pk).exists()

    def test_executions_go_with_their_collected_variables(self, victim: dict[str, Any]) -> None:
        erase(victim)

        assert not FlowExecution.objects.unscoped().filter(pk=victim["execution"].pk).exists()

    def test_enrollments_go(self, victim: dict[str, Any]) -> None:
        erase(victim)

        assert not SequenceEnrollment.objects.unscoped().filter(pk=victim["enrollment"].pk).exists()

    def test_handled_comments_are_deleted_not_merely_unlinked(self, victim: dict[str, Any]) -> None:
        """The ``SET_NULL`` trap.

        A cascade would leave this row behind with ``commenter_ref`` intact —
        the commenter's platform user id, which is exactly the identifier the
        rest of the erasure removes.
        """
        erase(victim)

        assert not HandledComment.objects.unscoped().filter(pk=victim["comment"].pk).exists()

    def test_queue_rows_naming_the_contact_are_deleted_not_just_cancelled(self, victim: dict[str, Any]) -> None:
        """``payload`` can quote a rendered message, and nothing cascades it."""
        erase(victim)

        assert not ScheduledAction.objects.unscoped().filter(contact_id=victim["contact"].pk).exists()

    def test_notifications_naming_the_contact_are_deleted(self, victim: dict[str, Any]) -> None:
        """The display name is baked into ``title`` and ``body`` at write time."""
        erase(victim)

        assert not Notification.objects.filter(pk=victim["notification"].pk).exists()

    def test_the_audit_record_counts_what_went(self, victim: dict[str, Any]) -> None:
        record = erase(victim)

        assert record.status == ErasureStatus.DONE
        assert record.counts["messaging.Message"] == 2
        assert record.counts["contacts.Contact"] == 1
        assert record.counts["flows.HandledComment"] == 1


# ---------------------------------------------------------------------------
# And nothing else
# ---------------------------------------------------------------------------


class TestAndNothingElse:
    def test_a_second_contact_in_the_same_workspace_is_untouched(
        self, victim: dict[str, Any], bystander: dict[str, Any]
    ) -> None:
        erase(victim)

        bystander["contact"].refresh_from_db()
        assert ContactChannelIdentity.objects.unscoped().filter(pk=bystander["identity"].pk).exists()
        assert Message.objects.unscoped().filter(conversation=bystander["conversation"]).count() == 2
        assert FlowExecution.objects.unscoped().filter(pk=bystander["execution"].pk).exists()
        assert ScheduledAction.objects.unscoped().filter(pk=bystander["action"].pk).exists()
        assert HandledComment.objects.unscoped().filter(pk=bystander["comment"].pk).exists()
        assert Notification.objects.filter(pk=bystander["notification"].pk).exists()

    def test_another_tenancy_is_untouched(self, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        mine = seed(tenancy.workspace, nonce="zqxmine", label="m", user=tenancy.owner)
        theirs = seed(other_tenancy.workspace, nonce="zqxtheirs", label="t", user=other_tenancy.owner)

        erase(mine)

        assert Contact.objects.unscoped().filter(pk=theirs["contact"].pk).exists()
        assert Message.objects.unscoped().filter(conversation=theirs["conversation"]).count() == 2

    def test_workspace_level_rows_survive(self, victim: dict[str, Any], tenancy: Tenancy) -> None:
        """A tag is workspace vocabulary. Erasing the last contact who carried
        one must not delete the tag itself — that would be a schema change
        performed by a privacy request."""
        erase(victim)

        victim["tag"].refresh_from_db()
        victim["field"].refresh_from_db()
        victim["connection"].refresh_from_db()
        victim["flow"].refresh_from_db()
        victim["sequence"].refresh_from_db()

    def test_the_email_suppression_survives(self, victim: dict[str, Any]) -> None:
        """Deliberate, and pinned in two places.

        ``apps/channels/models.py`` argues it and
        ``apps/channels/tests/test_email_suppression.py`` already asserts it
        from the other side: the list is keyed on the mailbox because a bounce
        is a fact about a mailbox, and deleting it would let a re-import mail
        somebody who complained.
        """
        erase(victim)

        assert EmailSuppression.objects.unscoped().filter(address="zqxvictim@example.test").exists()


# ---------------------------------------------------------------------------
# The DB-level sweep
# ---------------------------------------------------------------------------


def contact_references() -> dict[str, str]:
    """Every reference to ``Contact`` in the project. ``{label.field: kind}``.

    Foreign keys by introspection, plus the one column that names a contact
    without being one — ``queueing.ScheduledAction.contact_id`` is a plain
    ``UUIDField`` by design, which is exactly why it needs naming here.
    """
    found: dict[str, str] = {}
    for model in django_apps.get_models():
        for field in model._meta.get_fields():
            if getattr(field, "many_to_many", False) or not getattr(field, "related_model", None):
                continue
            if field.related_model is not Contact or not field.concrete:
                continue
            on_delete = getattr(field.remote_field, "on_delete", None)
            found[f"{model._meta.label}.{field.name}"] = getattr(on_delete, "__name__", "unknown")
    found["queueing.ScheduledAction.contact_id"] = "none"
    return found


#: Every way a row can name a contact, and what erasure does about it.
#:
#: The point of the dict is the test below it: a model that grows a reference to
#: ``Contact`` and is not listed here turns the suite red, so somebody has to
#: decide what erasure should do about it rather than discovering later that the
#: answer was "nothing". Same mechanism as ``WRITE_SITES`` and ``WAIVED_ROUTES``.
CLASSIFIED: dict[str, str] = {
    # Cascades from Contact.delete(). Nothing to write here, and deliberately
    # nothing written: re-spelling a cascade in Python is a second description
    # of a rule the database already enforces.
    "contacts.ContactTag.contact": "cascade",
    "contacts.CustomFieldValue.contact": "cascade",
    "messaging.ContactChannelIdentity.contact": "cascade",
    "messaging.Conversation.contact": "cascade",
    "flows.FlowExecution.contact": "cascade",
    "flows.DefaultReplyState.contact": "cascade",
    "campaigns.SequenceEnrollment.contact": "cascade",
    "campaigns.RuleTriggerFire.contact": "cascade",
    # SET_NULL, and the row keeps a platform user id. Deleted by hand in
    # apps/flows/erasure.py.
    "flows.HandledComment.contact": "erased_by_hand",
    # No foreign key at all; payload and last_error can quote a message.
    # Deleted by hand in apps/queueing/registry.py::purge_for_contact.
    "queueing.ScheduledAction.contact_id": "erased_by_hand",
    # SET_NULL on purpose: an anonymised counter that has to outlive the person
    # (SPEC §19), settled first so the figures still reconcile.
    "broadcasts.BroadcastRecipient.contact": "anonymized",
}


class TestEveryContactReferenceHasBeenClassified:
    def test_the_model_graph_matches_the_table(self) -> None:
        assert set(contact_references()) == set(CLASSIFIED), (
            "A model gained or lost a reference to Contact. Add it to CLASSIFIED with the kind of "
            "treatment erasure gives it, and make sure that treatment exists."
        )

    def test_every_kind_is_one_the_erasure_implements(self) -> None:
        assert set(CLASSIFIED.values()) <= {"cascade", "erased_by_hand", "anonymized"}

    def test_the_cascading_ones_really_cascade(self) -> None:
        """A ``cascade`` classification that is really ``SET_NULL`` would leave
        rows behind and this table would say otherwise."""
        graph_kinds = contact_references()
        for label, kind in CLASSIFIED.items():
            if kind == "cascade":
                assert graph_kinds[label] == "CASCADE", label


class TestNoPiiSurvivesAnywhere:
    """The acceptance criterion's DB-level sweep."""

    #: Text that survives, with the reason. Every entry is a decision somebody
    #: made on the record, not an omission.
    RETAINED = {
        "channels.EmailSuppression": (
            "The mailbox bounced or reported us as spam. Keyed on the address with no contact FK, "
            "so a re-import cannot undo it (apps/channels/models.py)."
        ),
    }

    def test_the_nonce_is_gone_from_every_text_and_json_column(
        self, victim: dict[str, Any], bystander: dict[str, Any]
    ) -> None:
        erase(victim)

        survivors = sorted(_models_containing("zqxvictim"))

        assert survivors == sorted(self.RETAINED)

    def test_the_bystanders_nonce_is_everywhere_it_was(self, victim: dict[str, Any], bystander: dict[str, Any]) -> None:
        """The control. Without it the sweep above passes on an empty database."""
        erase(victim)

        survivors = _models_containing("zqxbystand")

        assert "contacts.Contact" in survivors
        assert "messaging.Message" in survivors
        assert "flows.HandledComment" in survivors

    def test_the_sweep_would_catch_a_survivor(self, victim: dict[str, Any], tenancy: Tenancy) -> None:
        """A test that can only pass is not a test.

        Plant the nonce somewhere erasure does not reach and prove the sweep
        reports it.
        """
        erase(victim)
        Notification.objects.create(
            user=tenancy.owner, event_type="x", title="zqxvictim left behind", body="", payload={}
        )

        assert "notifications.Notification" in _models_containing("zqxvictim")


def _models_containing(needle: str) -> set[str]:
    """Every model with ``needle`` in any text or JSON column.

    Deliberately introspective rather than a hand-written list of columns: the
    list is what goes stale, and a column added next year is exactly the one
    that would hold something nobody swept.
    """
    from django.db.models import CharField, EmailField, JSONField, Q, TextField, URLField

    hits: set[str] = set()
    for model in django_apps.get_models():
        predicates = Q()
        matched = False
        for field in model._meta.get_fields():
            if not getattr(field, "concrete", False):
                continue
            searchable = isinstance(field, JSONField) or (
                # ``choices`` excludes enum columns: a status of "deleted" is
                # not text anybody typed, and matching on one would make the
                # sweep report a model for a value it was always going to hold.
                isinstance(field, CharField | TextField | EmailField | URLField) and not field.choices
            )
            if not searchable:
                continue
            predicates |= Q(**{f"{field.name}__icontains": needle})
            matched = True
        if not matched:
            continue
        manager = getattr(model, "all_objects", model._default_manager)
        if manager.filter(predicates).exists():
            hits.add(model._meta.label)
    return hits


# ---------------------------------------------------------------------------
# In-flight work
# ---------------------------------------------------------------------------


class TestInFlightWork:
    """The issue's third GDPR bullet: live executions expired first, pending
    actions cancelled, broadcast fanout rows skipped gracefully."""

    def test_a_live_execution_is_expired_before_the_rows_go(self, victim: dict[str, Any]) -> None:
        """Expired, not merely deleted by the cascade.

        The order is what matters: ``stand_down`` runs while the contact is
        still something the engine will accept, so the queue rows that would
        have resumed the run are cancelled too. A cascade alone would delete the
        execution and leave those armed.
        """
        erase(victim)

        assert not FlowExecution.objects.unscoped().filter(pk=victim["execution"].pk).exists()
        assert not ScheduledAction.objects.unscoped().filter(contact_id=victim["contact"].pk).exists()

    def test_a_pending_broadcast_recipient_is_skipped_not_deleted(self, victim: dict[str, Any]) -> None:
        """ "Skipped gracefully", and the row survives to say so."""
        sending = Broadcast.objects.create(
            workspace=victim["contact"].workspace,
            channel_connection=victim["connection"],
            name="In flight",
            status=BroadcastStatus.SENDING,
        )
        pending = BroadcastRecipient.objects.create(
            workspace=victim["contact"].workspace,
            broadcast=sending,
            contact=victim["contact"],
            status=RecipientStatus.PENDING,
        )

        erase(victim)

        pending.refresh_from_db()
        assert pending.status == RecipientStatus.SKIPPED
        assert pending.reason == "contact_deleted"
        assert pending.contact_id is None

    def test_a_sending_broadcast_settles_once_its_last_recipient_is_erased(self, victim: dict[str, Any]) -> None:
        """Otherwise it sits at ``sending`` until the hourly sweep notices — and
        its frozen ``stats`` is not written until it settles."""
        sending = Broadcast.objects.create(
            workspace=victim["contact"].workspace,
            channel_connection=victim["connection"],
            name="Last one out",
            status=BroadcastStatus.SENDING,
        )
        BroadcastRecipient.objects.create(
            workspace=victim["contact"].workspace,
            broadcast=sending,
            contact=victim["contact"],
            status=RecipientStatus.PENDING,
        )

        erase(victim)

        sending.refresh_from_db()
        assert sending.status == BroadcastStatus.SENT

    def test_a_finished_broadcasts_counters_still_reconcile(self, victim: dict[str, Any]) -> None:
        """SPEC §19's "keep anonymized counters", and the acceptance criterion.

        The recipient row is the *only* counter in the product that names a
        contact. ``services.counters`` recomputes a settled broadcast's figures
        from these rows live while the list page reads the frozen ``stats`` json
        — so a cascade would make one page disagree with the other about a
        broadcast that was sent last month.
        """
        from apps.broadcasts import services as broadcast_services

        before = broadcast_services.counters(victim["broadcast"])

        erase(victim)

        after = broadcast_services.counters(victim["broadcast"])
        assert after.queued == before.queued == 1
        assert after.sent == before.sent
        victim["broadcast"].refresh_from_db()
        assert victim["broadcast"].stats == {"queued": 1, "sent": 1, "failed": 0, "skipped": 0}

    def test_the_recipient_row_keeps_no_personal_data(self, victim: dict[str, Any]) -> None:
        """What survives is a counter, not a person."""
        erase(victim)

        victim["recipient"].refresh_from_db()
        assert victim["recipient"].contact_id is None
        assert victim["recipient"].identity_id is None
        assert victim["recipient"].message_id is None
        assert victim["recipient"].status == RecipientStatus.SENT


# ---------------------------------------------------------------------------
# The audit record
# ---------------------------------------------------------------------------


class TestTheAuditRecord:
    def test_it_names_who_and_when(self, victim: dict[str, Any], tenancy: Tenancy) -> None:
        record = erasure.begin(victim["contact"], source=ErasureSource.UI, requested_by=tenancy.owner)

        assert record.requested_by_id == tenancy.owner.pk
        assert record.requested_by_label == tenancy.owner.email
        assert record.source == ErasureSource.UI
        assert record.completed_at is not None

    def test_it_survives_the_contact_it_records(self, victim: dict[str, Any]) -> None:
        # Captured first: ``Model.delete()`` clears ``pk`` on the in-memory
        # instance, so reading it afterwards compares against None.
        erased = victim["contact"].pk

        record = erase(victim)

        record.refresh_from_db()
        assert record.contact_id == erased

    def test_it_holds_no_identifying_text(self, victim: dict[str, Any], tenancy: Tenancy) -> None:
        """The sharpest trap in the feature.

        An audit row that names the erased person makes the audit log the one
        place the erasure did not reach — and makes "no PII survives in any
        model" unprovable in principle. The contact id is pseudonymous: after
        the delete it resolves to nothing.
        """
        record = erase(victim)

        blob = " ".join(
            [record.requested_by_label, record.source, record.error, str(record.counts), str(record.contact_id)]
        )
        assert "zqxvictim" not in blob

    def test_a_second_live_erasure_is_refused_rather_than_racing(self, victim: dict[str, Any]) -> None:
        """A double-clicked button, and the partial unique constraint behind it."""
        erasure.begin(victim["contact"], source=ErasureSource.UI, force_queue=True)

        with pytest.raises(erasure.ErasureRefusedError):
            erasure.begin(victim["contact"], source=ErasureSource.UI, force_queue=True)

    def test_it_is_not_registered_in_the_admin(self) -> None:
        """``apps/contacts/admin.py`` registers ``Segment`` alone, and this row
        carries a contact id — the admin is not where that belongs."""
        from django.contrib import admin

        assert ContactErasure not in admin.site._registry


# ---------------------------------------------------------------------------
# The queued path
# ---------------------------------------------------------------------------


class TestTheQueuedPath:
    def test_a_forced_queue_tombstones_immediately_and_defers_the_rest(self, victim: dict[str, Any]) -> None:
        """ "Delete → export 404s" has to hold from the moment the request is
        accepted, not from the moment a worker gets to it."""
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)

        victim["contact"].refresh_from_db()
        assert record.status == ErasureStatus.PENDING
        assert victim["contact"].status == "deleted"
        assert Message.objects.unscoped().filter(conversation=victim["conversation"]).count() == 2
        assert ScheduledAction.objects.unscoped().filter(type=erasure.ACTION_TYPE).count() == 1

    def test_the_handler_finishes_it(self, victim: dict[str, Any]) -> None:
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)
        action = ScheduledAction.objects.unscoped().get(type=erasure.ACTION_TYPE)

        erasure.handle_contact_erasure({"erasure_id": str(record.pk)}, action)

        record.refresh_from_db()
        assert record.status == ErasureStatus.DONE
        assert not Contact.objects.unscoped().filter(pk=victim["contact"].pk).exists()

    def test_the_erasures_own_queue_row_survives_the_purge(self, victim: dict[str, Any]) -> None:
        """The action names the contact it is erasing. A purge that took it
        would delete the row the worker is holding open."""
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)
        action = ScheduledAction.objects.unscoped().get(type=erasure.ACTION_TYPE)

        erasure.handle_contact_erasure({"erasure_id": str(record.pk)}, action)

        assert ScheduledAction.objects.unscoped().filter(pk=action.pk).exists()

    def test_running_it_twice_is_a_no_op(self, victim: dict[str, Any]) -> None:
        """SPEC §15 retries. A second pass must not raise its way onto the
        backoff ladder for work that is already done."""
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)
        action = ScheduledAction.objects.unscoped().get(type=erasure.ACTION_TYPE)
        erasure.handle_contact_erasure({"erasure_id": str(record.pk)}, action)

        erasure.handle_contact_erasure({"erasure_id": str(record.pk)}, action)

        record.refresh_from_db()
        assert record.status == ErasureStatus.DONE

    def test_a_vanished_record_is_logged_rather_than_retried(self, victim: dict[str, Any]) -> None:
        action = ScheduledAction.objects.create(
            workspace=victim["contact"].workspace,
            contact_id=victim["contact"].pk,
            run_at=timezone.now(),
            type=erasure.ACTION_TYPE,
        )

        erasure.handle_contact_erasure({"erasure_id": str(uuid.uuid4())}, action)  # must not raise

    def test_a_small_contact_runs_inline(self, victim: dict[str, Any]) -> None:
        record = erase(victim)

        assert record.status == ErasureStatus.DONE
        assert not ScheduledAction.objects.unscoped().filter(type=erasure.ACTION_TYPE).exists()


# ---------------------------------------------------------------------------
# The CRM surface
# ---------------------------------------------------------------------------


def erase_url(tenancy: Tenancy, contact: Any) -> str:
    return f"/w/{tenancy.workspace.pk}/contacts/{contact.pk}/erase/"


def bulk_erase_url(tenancy: Tenancy) -> str:
    return f"/w/{tenancy.workspace.pk}/contacts/bulk/erase/"


def confirmed(contact: Any) -> dict[str, str]:
    return {"confirm": erasure.CONFIRMATION, "contact_id": str(contact.pk)}


class TestGating:
    """Admin only. ``manage_crm`` holds the reversible delete; this is the other
    one."""

    @pytest.mark.parametrize("role", ["editor", "agent", "viewer"])
    def test_everyone_below_admin_is_refused(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any], role: str
    ) -> None:
        contact = victim["contact"]

        response = client_for(tenancy.user_for(role)).post(erase_url(tenancy, contact), confirmed(contact))

        assert response.status_code == 403
        assert Contact.objects.unscoped().filter(pk=contact.pk).exists()

    def test_an_admin_can(self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]) -> None:
        contact = victim["contact"]

        response = client_for(tenancy.user_for("admin")).post(erase_url(tenancy, contact), confirmed(contact))

        assert response.status_code == 204
        assert not Contact.objects.unscoped().filter(pk=contact.pk).exists()

    def test_another_tenant_gets_404_not_403(
        self, tenancy: Tenancy, other_tenancy: Tenancy, client_for: Any, victim: dict[str, Any]
    ) -> None:
        """A 403 would confirm the id names something real."""
        contact = victim["contact"]

        response = client_for(other_tenancy.owner).post(erase_url(tenancy, contact), confirmed(contact))

        assert response.status_code == 404
        assert Contact.objects.unscoped().filter(pk=contact.pk).exists()

    def test_the_danger_zone_renders_only_for_an_admin(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]
    ) -> None:
        detail = f"/w/{tenancy.workspace.pk}/contacts/{victim['contact'].pk}/"

        assert b"Erase permanently" in client_for(tenancy.user_for("admin")).get(detail).content
        assert b"Erase permanently" not in client_for(tenancy.user_for("editor")).get(detail).content


class TestConfirmation:
    """Server-side, because ``hx-confirm`` is a ``window.confirm``."""

    def test_a_missing_confirmation_changes_nothing(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]
    ) -> None:
        contact = victim["contact"]

        client_for(tenancy.owner).post(erase_url(tenancy, contact), {"contact_id": str(contact.pk)})

        assert Contact.objects.unscoped().filter(pk=contact.pk).exists()

    def test_the_wrong_word_changes_nothing(self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]) -> None:
        contact = victim["contact"]

        client_for(tenancy.owner).post(erase_url(tenancy, contact), {"confirm": "erase", "contact_id": str(contact.pk)})

        assert Contact.objects.unscoped().filter(pk=contact.pk).exists()

    def test_a_stale_pane_posting_the_wrong_contact_id_changes_nothing(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any], bystander: dict[str, Any]
    ) -> None:
        """The check that actually matters. The CRM swaps panes with htmx, so a
        form built for one contact can be submitted at another's URL."""
        client_for(tenancy.owner).post(
            erase_url(tenancy, victim["contact"]),
            {"confirm": erasure.CONFIRMATION, "contact_id": str(bystander["contact"].pk)},
        )

        assert Contact.objects.unscoped().filter(pk=victim["contact"].pk).exists()
        assert Contact.objects.unscoped().filter(pk=bystander["contact"].pk).exists()


class TestBulkErase:
    def test_it_queues_the_selection(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any], bystander: dict[str, Any]
    ) -> None:
        """Always queued, whatever the size: five hundred teardowns is never a
        web request."""
        ids = [str(victim["contact"].pk), str(bystander["contact"].pk)]

        client_for(tenancy.owner).post(bulk_erase_url(tenancy), {"ids": ids, "confirm": erasure.CONFIRMATION})

        assert ContactErasure.objects.for_workspace(tenancy.workspace).count() == 2
        assert ScheduledAction.objects.unscoped().filter(type=erasure.ACTION_TYPE).count() == 2

    def test_it_refuses_without_the_sentinel(self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]) -> None:
        client_for(tenancy.owner).post(bulk_erase_url(tenancy), {"ids": [str(victim["contact"].pk)]})

        assert not ContactErasure.objects.for_workspace(tenancy.workspace).exists()

    def test_another_tenants_ids_are_simply_absent(
        self, tenancy: Tenancy, other_tenancy: Tenancy, client_for: Any
    ) -> None:
        """A miss, not a refusal — the house answer for an id in a POST body."""
        theirs = seed(other_tenancy.workspace, nonce="zqxbulk", label="bk", user=other_tenancy.owner)

        client_for(tenancy.owner).post(
            bulk_erase_url(tenancy), {"ids": [str(theirs["contact"].pk)], "confirm": erasure.CONFIRMATION}
        )

        assert Contact.objects.unscoped().filter(pk=theirs["contact"].pk).exists()
        assert not ContactErasure.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("role", ["editor", "agent", "viewer"])
    def test_it_is_admin_only_too(self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any], role: str) -> None:
        response = client_for(tenancy.user_for(role)).post(
            bulk_erase_url(tenancy), {"ids": [str(victim["contact"].pk)], "confirm": erasure.CONFIRMATION}
        )

        assert response.status_code == 403


class TestAFailedRunDoesNotStrandTheContact:
    """The worst state this feature has is an erasure that was requested, was
    not performed, and cannot be asked for again — a tombstone with the personal
    data still under it. Both paths have to come back from a failure.
    """

    @staticmethod
    def _explode(monkeypatch: Any, message: str = "the database went away") -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(message)

        monkeypatch.setattr(erasure.activity, "tear_down", boom)

    def test_an_inline_failure_is_recorded(self, victim: dict[str, Any], monkeypatch: Any) -> None:
        """The inline path has no queue row behind it, so nothing comes back for
        it later. It marks its own failure, outside the transaction that just
        rolled back."""
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            erase(victim)

        record = ContactErasure.objects.for_workspace(victim["contact"].workspace).get()
        assert record.status == ErasureStatus.FAILED
        assert "the database went away" in record.error

    def test_an_inline_failure_can_be_retried(self, victim: dict[str, Any], monkeypatch: Any) -> None:
        """The point of recording it. A refusal that outlives the run it was
        protecting is a contact nobody can ever erase."""
        self._explode(monkeypatch)
        with pytest.raises(RuntimeError):
            erase(victim)
        monkeypatch.undo()

        record = erase(victim)

        assert record.status == ErasureStatus.DONE
        assert not Contact.objects.unscoped().filter(pk=record.contact_id).exists()

    def test_the_queued_handler_does_not_pretend_to_record_a_failure(
        self, victim: dict[str, Any], monkeypatch: Any
    ) -> None:
        """``process_action`` runs the handler inside ``transaction.atomic()``,
        so a status written there and then re-raised is rolled back with
        everything else. It would look right in review and do nothing in
        production, so the handler does not try — the worker records the failure
        on the action instead."""
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)
        action = ScheduledAction.objects.unscoped().get(type=erasure.ACTION_TYPE)
        self._explode(monkeypatch)

        with pytest.raises(RuntimeError):
            erasure.handle_contact_erasure({"erasure_id": str(record.pk)}, action)

        record.refresh_from_db()
        assert record.status == ErasureStatus.PENDING

    def test_a_stalled_queued_erasure_is_reclaimed_by_the_next_request(
        self, victim: dict[str, Any], monkeypatch: Any
    ) -> None:
        """How the queued path recovers. Its action has given up, so the record
        is no longer live and must stop blocking."""
        record = erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)
        action = ScheduledAction.objects.unscoped().get(type=erasure.ACTION_TYPE)
        action.status = ActionStatus.FAILED
        action.save(update_fields=["status"])

        second = erasure.begin(victim["contact"], source=ErasureSource.UI)

        record.refresh_from_db()
        assert record.status == ErasureStatus.FAILED
        assert second.status == ErasureStatus.DONE

    def test_a_genuinely_live_erasure_still_refuses(self, victim: dict[str, Any]) -> None:
        """The reclaim must not become a way around the refusal: an action that
        is still armed means the work really is in flight."""
        erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)

        with pytest.raises(erasure.ErasureRefusedError):
            erasure.begin(victim["contact"], source=ErasureSource.UI)

    def test_the_database_refuses_a_duplicate_even_without_the_probe(self, victim: dict[str, Any]) -> None:
        """``erasure_one_live_per_contact``. The probe is a check-then-create, so
        two concurrent requests can both pass it and the constraint is what
        actually arbitrates."""
        erasure.begin(victim["contact"], source=ErasureSource.BULK, force_queue=True)

        with pytest.raises(IntegrityError):
            ContactErasure.objects.create(
                workspace=victim["contact"].workspace,
                contact_id=victim["contact"].pk,
                source=ErasureSource.API,
                status=ErasureStatus.PENDING,
            )

    def test_a_finished_erasure_does_not_block_a_later_one(self, victim: dict[str, Any]) -> None:
        """The constraint is partial for this reason: a contact erased once
        keeps a ``done`` receipt for ever, and a re-imported contact reusing the
        id must not be refused by it."""
        first = erase(victim)

        ContactErasure.objects.create(
            workspace=first.workspace,
            contact_id=first.contact_id,
            source=ErasureSource.UI,
            status=ErasureStatus.PENDING,
        )

    def test_the_recorded_error_is_scrubbed(self, victim: dict[str, Any], monkeypatch: Any) -> None:
        """A traceback quotes what it was working on, and this column is read in
        an audit — the discipline ``FlowExecution.last_error`` already applies.

        The fixture is assembled from parts and made of a repeating pattern, for
        the two reasons this repo has met before: GitHub push protection matches
        a contiguous provider-shaped literal, and gitleaks' ``generic-api-key``
        matches a high-entropy run near a credential keyword.
        """
        credential = "sk" + "_live_" + "deadbeef" * 2
        self._explode(monkeypatch, f"provider refused: {credential}")

        with pytest.raises(RuntimeError):
            erase(victim)

        record = ContactErasure.objects.for_workspace(victim["contact"].workspace).get()
        assert credential not in record.error
        assert "[REDACTED]" in record.error


class TestQueuedWebhookDeliveries:
    """The queue row a contact id does not appear on."""

    def test_a_pending_delivery_naming_the_contact_is_purged(self, victim: dict[str, Any]) -> None:
        """``enqueue_delivery`` deliberately leaves ``contact_id`` null so a slow
        receiver cannot stall everything else for that contact, and puts the id
        in ``payload["data"]`` instead. Matching only the column would leave a
        delivery that fires *after* the erasure, announcing an event naming a
        contact this deployment has promised to have forgotten.
        """
        pending = ScheduledAction.objects.create(
            workspace=victim["contact"].workspace,
            contact_id=None,
            run_at=timezone.now(),
            type="webhook_delivery",
            payload={
                "webhook_id": str(uuid.uuid4()),
                "event": "contact.tag_added",
                "data": {"contact_id": str(victim["contact"].pk), "tag_id": str(victim["tag"].pk)},
            },
            status=ActionStatus.PENDING,
        )

        erase(victim)

        assert not ScheduledAction.objects.unscoped().filter(pk=pending.pk).exists()

    def test_another_contacts_delivery_is_left_alone(self, victim: dict[str, Any], bystander: dict[str, Any]) -> None:
        theirs = ScheduledAction.objects.create(
            workspace=victim["contact"].workspace,
            contact_id=None,
            run_at=timezone.now(),
            type="webhook_delivery",
            payload={"event": "contact.tag_added", "data": {"contact_id": str(bystander["contact"].pk)}},
            status=ActionStatus.PENDING,
        )

        erase(victim)

        assert ScheduledAction.objects.unscoped().filter(pk=theirs.pk).exists()

    def test_a_delivery_with_no_contact_at_all_is_left_alone(self, victim: dict[str, Any]) -> None:
        """``broadcast.finished`` names no contact and must survive."""
        unrelated = ScheduledAction.objects.create(
            workspace=victim["contact"].workspace,
            contact_id=None,
            run_at=timezone.now(),
            type="webhook_delivery",
            payload={"event": "broadcast.finished", "data": {"broadcast_id": str(victim["broadcast"].pk)}},
            status=ActionStatus.PENDING,
        )

        erase(victim)

        assert ScheduledAction.objects.unscoped().filter(pk=unrelated.pk).exists()


class TestTheBroadcastRecipientsPage:
    def test_it_renders_an_erased_recipient(self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]) -> None:
        """The anonymised counter row has a null contact, and the page reverses
        the contact-detail URL from it. A null id there is a NoReverseMatch,
        which is a 500 on the default (skipped) recipient list."""
        broadcast = victim["broadcast"]
        erase(victim)

        response = client_for(tenancy.owner).get(
            f"/w/{tenancy.workspace.pk}/broadcasts/{broadcast.pk}/recipients/?status=sent"
        )

        assert response.status_code == 200
        assert b"Erased contact" in response.content

    def test_the_skipped_list_renders_after_a_mid_flight_erasure(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]
    ) -> None:
        """``skipped`` is the default tab, and erasing a pending recipient is
        exactly what puts an anonymised row in it."""
        sending = Broadcast.objects.create(
            workspace=tenancy.workspace,
            channel_connection=victim["connection"],
            name="In flight",
            status=BroadcastStatus.SENDING,
        )
        BroadcastRecipient.objects.create(
            workspace=tenancy.workspace,
            broadcast=sending,
            contact=victim["contact"],
            status=RecipientStatus.PENDING,
        )
        erase(victim)

        response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/broadcasts/{sending.pk}/recipients/")

        assert response.status_code == 200
        assert b"Erased contact" in response.content


class TestBulkEraseTombstones:
    def test_it_cannot_reach_a_tombstone_the_way_the_detail_page_can(
        self, tenancy: Tenancy, client_for: Any, victim: dict[str, Any]
    ) -> None:
        """A documented asymmetry, not an oversight.

        ``_selected`` filters to active contacts because that is what the list
        can select; widening it would widen ``bulk_tag`` and ``bulk_delete``
        too. The single-contact route reaches a tombstone, and so does the API.
        """
        from apps.contacts.services import delete_contact

        delete_contact(victim["contact"])

        client_for(tenancy.owner).post(
            bulk_erase_url(tenancy),
            {"ids": [str(victim["contact"].pk)], "confirm": erasure.CONFIRMATION},
        )

        assert not ContactErasure.objects.for_workspace(tenancy.workspace).exists()
        assert Contact.objects.unscoped().filter(pk=victim["contact"].pk).exists()
