"""SPEC §9.3's wait/resume matrix, one row at a time.

The rule this file exists to pin is the two-way one:

    unmatched input with no retry -> execution keeps waiting AND the event falls
    through to (3) trigger matching […] matched or retried input is consumed.

So every test asserts *both* halves: what came back to L4-A, and what state the
execution is in afterwards. A test that only checked one of the two would pass
for an implementation that expires the execution on an unmatched reply, which is
the mistake worth designing out.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.contacts.services import create_custom_field, field_values_for
from apps.flows.engine import Consumed, NotConsumed, attempt_resume, start_flow
from apps.flows.engine.waits import (
    InvalidAnswerError,
    buttons_wait,
    data_collection_wait,
    deadline,
    normalise_answer,
)
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import (
    FakeFacade,
    connection_for,
    contact_for,
    edge,
    graph,
    inbound,
    node,
    published_flow,
)


def _tagger(name: str) -> dict:
    return {"actions": [{"verb": "add_tag", "tag": name}]}


def _question(**overrides) -> dict:
    config = {
        "question": "What is your email?",
        "reply_type": "email",
        "target": {"type": "system_field", "key": "email"},
    }
    config.update(overrides)
    return config


def _choice_flow(workspace, *, buttons=None, quick_replies=None, followup=None, retry=None):
    """A send_message that asks, with a landing node behind every handle."""
    config: dict = {"blocks": [{"type": "text", "text": "Pick one:"}]}
    if buttons:
        config["buttons"] = buttons
    if quick_replies:
        config["quick_replies"] = quick_replies
    if followup:
        config["followup"] = followup
    if retry:
        config["retry_unmatched"] = retry

    nodes = [node("ask", "send_message", config)]
    edges = []
    for index, button in enumerate(buttons or []):
        nodes.append(node(f"b-{button['id']}", "action", _tagger(f"btn-{button['id']}"), x=200 * (index + 1)))
        edges.append(edge("ask", f"btn:{button['id']}", f"b-{button['id']}"))
    for index, reply in enumerate(quick_replies or []):
        nodes.append(node(f"q-{reply['id']}", "action", _tagger(f"qr-{reply['id']}"), x=200 * (index + 5)))
        edges.append(edge("ask", f"qr:{reply['id']}", f"q-{reply['id']}"))
    nodes.append(node("timed-out", "action", _tagger("timed-out"), x=2000))
    edges.append(edge("ask", "timeout", "timed-out"))
    return published_flow(workspace, graph(nodes, edges))


@pytest.fixture
def facade(monkeypatch):
    return FakeFacade().install(monkeypatch)


def _parked(workspace, flow, connection):
    contact = contact_for(workspace)
    execution = start_flow(contact, flow, started_by=StartedBy.API, connection=connection)
    assert execution.status == ExecutionStatus.WAITING_REPLY
    return contact, execution


def _tags(contact) -> set[str]:
    return {tag.name for tag in contact.tags.all()}


@pytest.mark.django_db
class TestButtonAndQuickReplyMatching:
    def test_a_button_id_matches(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, buttons=[{"id": "yes", "label": "Yes", "action": "postback"}])
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, button_id="yes"))

        assert isinstance(outcome, Consumed)
        assert outcome.execution.status == ExecutionStatus.COMPLETED
        assert _tags(contact) == {"btn-yes"}

    def test_a_quick_reply_id_matches(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, quick_replies=[{"id": "later", "label": "Later"}])
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, button_id="later"))

        assert isinstance(outcome, Consumed)
        assert _tags(contact) == {"qr-later"}

    def test_a_quick_reply_label_matches_as_plain_text(self, tenancy, facade):
        """Telegram reply keyboards come back as a message, not a callback id."""
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, quick_replies=[{"id": "later", "label": "Later"}])
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="  later "))

        assert isinstance(outcome, Consumed)
        assert _tags(contact) == {"qr-later"}

    def test_a_rendered_label_is_what_gets_matched(self, tenancy, facade):
        """The contact can only type what they were shown."""
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, quick_replies=[{"id": "hi", "label": "Yes, {{first_name}}"}])
        contact, execution = _parked(tenancy.workspace, flow, connection)

        assert execution.wait_config["labels"] == {"yes, ada": "hi"}
        assert isinstance(attempt_resume(execution, inbound(connection, text="Yes, Ada")), Consumed)

    def test_a_url_button_is_not_an_expected_reply(self, tenancy, facade):
        """It opens a link; nothing ever comes back for it."""
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[
                {"id": "docs", "label": "Docs", "action": "url", "url": "https://example.test/"},
                {"id": "talk", "label": "Talk", "action": "postback"},
            ],
        )
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        assert set(execution.wait_config["handles"]) == {"talk"}


@pytest.mark.django_db
class TestUnmatchedInput:
    def test_with_no_retry_it_keeps_waiting_and_falls_through(self, tenancy, facade):
        """The §9.3 rule in one test: not consumed, still parked."""
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, buttons=[{"id": "yes", "label": "Yes", "action": "postback"}])
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="something else"))

        assert isinstance(outcome, NotConsumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.current_node_id == "ask"
        assert _tags(contact) == set()

    def test_with_retry_it_re_asks_and_consumes(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            retry={"enabled": True, "max": 2, "text": "Please pick one."},
        )
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="nope"))

        assert isinstance(outcome, Consumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.wait_config["retry"]["count"] == 1
        sends = facade.named("send_outbound")
        assert sends[-1]["outbound"].blocks[0].text == "Please pick one."

    def test_the_retry_budget_runs_out_and_then_it_falls_through(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            retry={"enabled": True, "max": 2, "text": "Please pick one."},
        )
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        assert isinstance(attempt_resume(execution, inbound(connection, text="a")), Consumed)
        assert isinstance(attempt_resume(execution, inbound(connection, text="b")), Consumed)
        spent = attempt_resume(execution, inbound(connection, text="c"))

        assert isinstance(spent, NotConsumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_each_retry_is_a_distinct_message(self, tenancy, facade):
        """SPEC §9.4's attempt bucket: a re-ask is not a duplicate of the first."""
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            retry={"enabled": True, "max": 2, "text": "Please pick one."},
        )
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="a"))
        attempt_resume(execution, inbound(connection, text="b"))

        keys = [call["idempotency_key"] for call in facade.named("send_outbound")]
        assert len(set(keys)) == len(keys)
        assert keys[-1].endswith(":2")

    def test_a_matching_reply_after_a_retry_still_works(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            retry={"enabled": True, "max": 3, "text": "Please pick one."},
        )
        contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="what"))
        outcome = attempt_resume(execution, inbound(connection, button_id="yes"))

        assert isinstance(outcome, Consumed)
        assert _tags(contact) == {"btn-yes"}


@pytest.mark.django_db
class TestWhatIsNotOffered:
    def test_an_execution_that_is_not_waiting_is_never_consumed(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = published_flow(tenancy.workspace, graph([node("a", "action", _tagger("ran"))]))
        contact = contact_for(tenancy.workspace)
        execution = start_flow(contact, flow, started_by=StartedBy.API, connection=connection)

        assert isinstance(attempt_resume(execution, inbound(connection, text="hi")), NotConsumed)

    def test_a_smart_delay_is_resumed_only_by_its_own_action(self, tenancy, facade):
        """SPEC §9.3, verbatim."""
        connection = connection_for(tenancy.workspace)
        document = graph([node("d", "smart_delay", {"mode": "duration", "duration": {"value": 2, "unit": "hours"}})])
        flow = published_flow(tenancy.workspace, document)
        contact = contact_for(tenancy.workspace)
        execution = start_flow(contact, flow, started_by=StartedBy.API, connection=connection)
        assert execution.status == ExecutionStatus.WAITING_DELAY

        assert isinstance(attempt_resume(execution, inbound(connection, text="hurry up")), NotConsumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_DELAY

    def test_an_event_on_another_channel_is_not_consumed(self, tenancy, facade):
        """SPEC §9.3 routes to the "waiting execution on that channel"."""
        telegram = connection_for(tenancy.workspace)
        instagram = connection_for(tenancy.workspace, platform="instagram", external_id="ig-1")
        flow = _choice_flow(tenancy.workspace, buttons=[{"id": "yes", "label": "Yes", "action": "postback"}])
        _contact, execution = _parked(tenancy.workspace, flow, telegram)

        outcome = attempt_resume(execution, inbound(instagram, button_id="yes"))

        assert isinstance(outcome, NotConsumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY


@pytest.mark.django_db
class TestFollowupTimeout:
    def test_the_deadline_arms_a_timer_that_takes_the_timeout_branch(self, tenancy, facade):
        from apps.queueing.models import ActionType, ScheduledAction
        from apps.queueing.registry import get_handler

        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            followup={"enabled": True, "delay": 1, "unit": "hours"},
        )
        contact, execution = _parked(tenancy.workspace, flow, connection)

        timer = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.FOLLOWUP_TIMER)
        assert timer.run_at > timezone.now()
        get_handler(ActionType.FOLLOWUP_TIMER)(timer.payload, timer)

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.COMPLETED
        assert _tags(contact) == {"timed-out"}

    def test_a_reply_first_makes_the_timer_a_no_op(self, tenancy, facade):
        """The token check. Both racing is the ordinary case, not an edge case."""
        from apps.queueing.models import ActionType, ScheduledAction
        from apps.queueing.registry import get_handler

        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(
            tenancy.workspace,
            buttons=[{"id": "yes", "label": "Yes", "action": "postback"}],
            followup={"enabled": True, "delay": 1, "unit": "hours"},
        )
        contact, execution = _parked(tenancy.workspace, flow, connection)
        timer = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.FOLLOWUP_TIMER)

        attempt_resume(execution, inbound(connection, button_id="yes"))
        get_handler(ActionType.FOLLOWUP_TIMER)(timer.payload, timer)

        assert _tags(contact) == {"btn-yes"}

    def test_no_followup_means_no_timer(self, tenancy, facade):
        from apps.queueing.models import ActionType, ScheduledAction

        connection = connection_for(tenancy.workspace)
        flow = _choice_flow(tenancy.workspace, buttons=[{"id": "yes", "label": "Yes", "action": "postback"}])
        _parked(tenancy.workspace, flow, connection)

        assert (
            not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.FOLLOWUP_TIMER).exists()
        )


@pytest.mark.django_db
class TestDataCollectionResume:
    def _flow(self, workspace, **overrides):
        document = graph(
            [
                node("ask", "data_collection", _question(**overrides)),
                node("done", "action", _tagger("answered"), x=200),
                node("gave-up", "action", _tagger("timed-out"), x=400),
            ],
            [edge("ask", "default", "done"), edge("ask", "timeout", "gave-up")],
        )
        return published_flow(workspace, document)

    def test_a_valid_answer_is_saved_and_the_flow_moves_on(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace)
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="  Ada@Example.test "))

        assert isinstance(outcome, Consumed)
        contact.refresh_from_db()
        assert contact.email == "ada@example.test"
        assert _tags(contact) == {"answered"}

    def test_an_email_answer_records_consent_through_the_facade(self, tenancy, facade):
        """SPEC §11.8's audit trail — the reason this goes through contract 1."""
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace)
        contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="ada@example.test"))

        consent = facade.named("upsert_contact_identity")
        assert consent == [
            {
                "contact": contact,
                "platform": "email",
                "address": "ada@example.test",
                "source": "data_collection",
                "opt_in": True,
            }
        ]

    def test_a_phone_answer_records_an_sms_identity(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, reply_type="phone", target={"type": "system_field", "key": "phone"})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="+1 (555) 010-1234"))

        contact.refresh_from_db()
        assert contact.phone == "+15550101234"
        assert facade.named("upsert_contact_identity")[0]["platform"] == "sms"

    def test_a_custom_field_target_is_written_typed(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        field = create_custom_field(tenancy.workspace, name="Budget", field_type="number")
        flow = self._flow(tenancy.workspace, reply_type="number", target={"type": "custom_field", "key": "budget"})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="1,250.50"))

        assert field_values_for(contact)[field.pk] == Decimal("1250.50")
        assert facade.named("upsert_contact_identity") == []

    def test_an_invalid_answer_re_asks_and_consumes(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, retry={"max": 2, "invalid_text": "Try again, {{first_name}}."})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="not an email"))

        assert isinstance(outcome, Consumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert facade.named("send_outbound")[-1]["outbound"].blocks[0].text == "Try again, Ada."
        contact.refresh_from_db()
        assert contact.email == ""

    def test_exhausted_retries_fall_through_and_keep_waiting(self, tenancy, facade):
        """Same rule as an unmatched button: the timeout timer is the escape."""
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, retry={"max": 1, "invalid_text": "Try again."})
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        assert isinstance(attempt_resume(execution, inbound(connection, text="no")), Consumed)
        spent = attempt_resume(execution, inbound(connection, text="still no"))

        assert isinstance(spent, NotConsumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_an_email_into_a_custom_field_still_records_consent(self, tenancy, facade):
        """SPEC §11.8's "also" clause is keyed on reply_type, not on the target.

        An address captured into a custom field is still an address the
        deployment holds, so ``contact.email`` and the identity row are written
        either way — otherwise every custom-field node would quietly collect
        addresses with no record of why they may be messaged.
        """
        connection = connection_for(tenancy.workspace)
        create_custom_field(tenancy.workspace, name="Work email", field_type="text")
        flow = self._flow(tenancy.workspace, target={"type": "custom_field", "key": "Work email"})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        attempt_resume(execution, inbound(connection, text="ada@example.test"))

        contact.refresh_from_db()
        assert contact.email == "ada@example.test"
        assert facade.named("upsert_contact_identity") == [
            {
                "contact": contact,
                "platform": "email",
                "address": "ada@example.test",
                "source": "data_collection",
                "opt_in": True,
            }
        ]

    def test_a_text_question_filed_under_email_records_no_consent(self, tenancy, facade):
        """The mirror: consent must never be recorded for an unvalidated reply."""
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, reply_type="text")
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="dunno"))

        assert isinstance(outcome, Consumed)
        contact.refresh_from_db()
        assert contact.email == "dunno"
        assert facade.named("upsert_contact_identity") == []

    def test_an_answer_too_long_for_the_column_is_invalid_not_a_crash(self, tenancy, facade):
        """`Contact.first_name` is 150 chars; answers are allowed 4096.

        Without a length check the save reaches Postgres and raises
        ``StringDataRightTruncation``, rolling the resume back and parking the
        execution while the queue burns its retries. Inbound text is
        attacker-controlled, so this has to be an ordinary invalid answer.
        """
        connection = connection_for(tenancy.workspace)
        flow = self._flow(
            tenancy.workspace,
            reply_type="text",
            target={"type": "system_field", "key": "first_name"},
            retry={"max": 2, "invalid_text": "Shorter, please."},
        )
        contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="x" * 200))

        assert isinstance(outcome, Consumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY
        contact.refresh_from_db()
        assert contact.first_name == "Ada"
        assert facade.named("send_outbound")[-1]["outbound"].blocks[0].text == "Shorter, please."

    def test_nothing_is_half_written_when_the_second_column_refuses(self, tenancy, facade):
        """An address that fits its custom field but not ``Contact.email``.

        Both writes are length-checked before either happens, so the refusal
        leaves the custom field untouched rather than committing it alongside
        the bumped retry counter.
        """
        connection = connection_for(tenancy.workspace)
        create_custom_field(tenancy.workspace, name="Work email", field_type="text")
        flow = self._flow(tenancy.workspace, target={"type": "custom_field", "key": "Work email"})
        contact, execution = _parked(tenancy.workspace, flow, connection)
        long_address = f"{'a' * 250}@example.test"

        outcome = attempt_resume(execution, inbound(connection, text=long_address))

        assert isinstance(outcome, Consumed)
        assert field_values_for(contact) == {}
        contact.refresh_from_db()
        assert contact.email == ""
        assert facade.named("upsert_contact_identity") == []

    def test_a_value_that_does_not_fit_the_field_type_re_asks(self, tenancy, facade):
        """A number question filed in a date field is the contact's problem to fix."""
        connection = connection_for(tenancy.workspace)
        create_custom_field(tenancy.workspace, name="Renewal", field_type="date")
        flow = self._flow(
            tenancy.workspace,
            reply_type="text",
            target={"type": "custom_field", "key": "Renewal"},
            retry={"max": 1, "invalid_text": "Use YYYY-MM-DD."},
        )
        _contact, execution = _parked(tenancy.workspace, flow, connection)

        outcome = attempt_resume(execution, inbound(connection, text="soon"))

        assert isinstance(outcome, Consumed)
        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_a_target_that_no_longer_exists_does_not_kill_the_run(self, tenancy, facade, caplog):
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, reply_type="text", target={"type": "custom_field", "key": "deleted"})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        with caplog.at_level("WARNING"):
            outcome = attempt_resume(execution, inbound(connection, text="something"))

        assert isinstance(outcome, Consumed)
        assert _tags(contact) == {"answered"}
        assert "no custom field named" in caplog.text

    def test_a_non_writable_system_field_is_refused(self, tenancy, facade, caplog):
        connection = connection_for(tenancy.workspace)
        flow = self._flow(tenancy.workspace, reply_type="text", target={"type": "system_field", "key": "status"})
        contact, execution = _parked(tenancy.workspace, flow, connection)

        with caplog.at_level("WARNING"):
            attempt_resume(execution, inbound(connection, text="deleted"))

        contact.refresh_from_db()
        assert contact.status == "active"
        assert "not a writable contact field" in caplog.text


class TestAnswerValidation:
    """SPEC §11.8's six reply types, valid and invalid."""

    @pytest.mark.parametrize(
        ("reply_type", "text", "expected"),
        [
            ("text", "  hello  ", "hello"),
            ("email", "Ada@Example.TEST", "ada@example.test"),
            ("phone", "+1 (555) 010-1234", "+15550101234"),
            ("phone", "555 010 1234", "5550101234"),
            ("number", "42", Decimal("42")),
            ("number", "1,250.50", Decimal("1250.50")),
            ("number", "-3.5", Decimal("-3.5")),
            ("date", "2026-08-22", date(2026, 8, 22)),
            ("url", "https://example.test/a?b=c", "https://example.test/a?b=c"),
        ],
    )
    def test_valid_answers_normalise(self, reply_type, text, expected):
        assert normalise_answer(text, reply_type) == expected

    @pytest.mark.parametrize(
        ("reply_type", "text"),
        [
            ("text", "   "),
            ("email", "ada@"),
            ("email", "not an email"),
            ("phone", "12345"),
            ("phone", "call me"),
            # str.isdigit() is true for these; they are not phone numbers.
            ("phone", "²²²²²²²"),
            ("phone", "٣٣٣٣٣٣٣"),
            ("number", "many"),
            ("number", "NaN"),
            ("date", "the third"),
            ("date", "2026-13-40"),
            ("url", "example.test"),
            ("url", "javascript:alert(1)"),
        ],
    )
    def test_invalid_answers_are_rejected(self, reply_type, text):
        with pytest.raises(InvalidAnswerError):
            normalise_answer(text, reply_type)

    def test_an_unknown_reply_type_is_rejected(self):
        with pytest.raises(InvalidAnswerError, match="is not a reply type"):
            normalise_answer("x", "postcode")

    def test_an_oversized_answer_is_rejected(self):
        with pytest.raises(InvalidAnswerError, match="too long"):
            normalise_answer("x" * 5000, "text")


class TestWaitBuilders:
    def test_buttons_and_quick_replies_share_one_handle_map(self):
        config = buttons_wait(
            "n1",
            buttons=[{"id": "a", "label": "A", "action": "postback"}],
            quick_replies=[{"id": "b", "label": "B"}],
        )

        assert config["handles"] == {"a": "btn:a", "b": "qr:b"}
        assert config["type"] == "buttons"
        assert "retry" not in config and "timeout" not in config

    def test_a_disabled_retry_block_is_dropped(self):
        config = buttons_wait("n1", retry_unmatched={"enabled": False, "max": 5, "text": "x"})
        assert "retry" not in config

    def test_the_retry_cap_is_enforced_at_runtime_too(self):
        """SPEC §11.1 caps it at 5; the schema does too, but a stored graph might not."""
        config = buttons_wait("n1", retry_unmatched={"enabled": True, "max": 99, "text": "x"})
        assert config["retry"]["max"] == 5

    def test_data_collection_defaults_to_three_retries(self):
        """SPEC §11.8: "retry limit (default 3)"."""
        config = data_collection_wait("n1", reply_type="text", target={"type": "system_field", "key": "email"})
        assert config["retry"] == {"max": 3, "count": 0, "text": ""}

    def test_a_deadline_becomes_an_instant(self):
        now = timezone.now()
        parsed = deadline({"enabled": True, "delay": 2, "unit": "hours"}, now=now)
        assert parsed is not None
        assert parsed["handle"] == "timeout"
        assert parsed["run_at"] == (now + timezone.timedelta(hours=2)).isoformat()

    @pytest.mark.parametrize(
        "block",
        [None, {}, {"enabled": False}, {"enabled": True}, {"enabled": True, "delay": 0, "unit": "hours"}],
    )
    def test_an_unusable_deadline_is_no_deadline(self, block):
        """A wait that never times out beats one that fires immediately."""
        assert deadline(block) is None
