"""``send_message``, ``smart_delay`` and ``data_collection`` as nodes.

The resume side of the last two lives in ``test_waits.py``; this is about what
each node does when the runner reaches it — what it renders, what it sends, what
it schedules, and which handle it takes when something goes wrong.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.contacts.services import create_custom_field, set_field_value
from apps.flows import messaging
from apps.flows.engine import start_flow
from apps.flows.engine.nodes.smart_delay import WEEKDAYS
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import (
    FakeFacade,
    FakeMessage,
    connection_for,
    contact_for,
    edge,
    graph,
    node,
    published_flow,
)
from apps.queueing.models import ActionType, ScheduledAction


@pytest.fixture
def facade(monkeypatch):
    return FakeFacade().install(monkeypatch)


def _tagger(name: str) -> dict:
    return {"actions": [{"verb": "add_tag", "tag": name}]}


def _run(workspace, document, *, connection=None, contact=None, **kwargs):
    flow = published_flow(workspace, document)
    contact = contact or contact_for(workspace)
    return contact, start_flow(contact, flow, started_by=StartedBy.API, connection=connection, **kwargs)


def _run_draft(workspace, document, *, connection=None, contact=None):
    """Run a graph the publish gate would reject, the way #12's preview can.

    The runtime guards below are unreachable through a published flow — the
    schema catches a smart_delay with no duration and a reply_type that is not
    one of the six. ``save_draft`` does not validate (SPEC §16 autosaves every
    two seconds, mid-edit), so a draft preview is exactly how a graph like this
    reaches the engine, and exactly why the guards exist.
    """
    from apps.flows.services import save_draft

    flow = published_flow(workspace, graph([node("ok", "action", _tagger("placeholder"))]))
    version = save_draft(flow, document)
    contact = contact or contact_for(workspace)
    return contact, start_flow(contact, flow, started_by=StartedBy.PREVIEW, flow_version=version, connection=connection)


def _sent(facade) -> object:
    calls = facade.named("send_outbound")
    assert calls, "nothing was sent"
    return calls[-1]["outbound"]


@pytest.mark.django_db
class TestSendMessage:
    def test_text_blocks_are_rendered_and_sent(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        document = graph([node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi {{first_name}}!"}]})])

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED
        assert _sent(facade).blocks[0].text == "Hi Ada!"

    def test_the_send_carries_the_spec_nine_four_idempotency_key(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        document = graph([node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi"}]})])

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        call = facade.named("send_outbound")[0]
        assert call["idempotency_key"] == f"exec:{execution.pk}:node:m:0"
        assert call["source"] == "automation"
        assert call["connection"] == connection

    def test_buttons_and_quick_replies_are_rendered_and_make_it_wait(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        config = {
            "blocks": [{"type": "text", "text": "Pick:"}],
            "buttons": [
                {
                    "id": "docs",
                    "label": "Docs for {{first_name}}",
                    "action": "url",
                    "url": "https://e.test/{{first_name}}",
                },
                {"id": "talk", "label": "Talk", "action": "postback"},
            ],
            "quick_replies": [{"id": "later", "label": "Later"}],
        }
        document = graph(
            [node("m", "send_message", config), node("t", "action", _tagger("talked"), x=200)],
            [edge("m", "btn:talk", "t")],
        )

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        outbound = _sent(facade)
        assert [button.label for button in outbound.buttons] == ["Docs for Ada", "Talk"]
        assert outbound.buttons[0].url == "https://e.test/Ada"
        assert [reply.label for reply in outbound.quick_replies] == ["Later"]
        assert execution.status == ExecutionStatus.WAITING_REPLY

    def test_url_only_buttons_do_not_park_the_execution(self, tenancy, facade):
        """A URL button opens a link and never replies, so waiting on one is a trap.

        The wait would carry an empty handle map, nothing the contact could ever
        send would match, and — with SPEC §22's one-live-execution-per-contact —
        that contact would be locked out of every flow until the 30-day sweep.
        """
        connection = connection_for(tenancy.workspace)
        config = {
            "blocks": [{"type": "text", "text": "Here are the docs"}],
            "buttons": [{"id": "docs", "label": "Docs", "action": "url", "url": "https://e.test/"}],
        }
        document = graph(
            [node("m", "send_message", config), node("n", "action", _tagger("next"), x=200)],
            [edge("m", "default", "n")],
        )

        contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"next"}

    def test_url_only_buttons_with_a_followup_still_wait(self, tenancy, facade):
        """Because now something *can* move it on: the timer the author armed."""
        connection = connection_for(tenancy.workspace)
        config = {
            "blocks": [{"type": "text", "text": "Here are the docs"}],
            "buttons": [{"id": "docs", "label": "Docs", "action": "url", "url": "https://e.test/"}],
            "followup": {"enabled": True, "delay": 1, "unit": "hours"},
        }
        document = graph([node("m", "send_message", config)])

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.wait_config["handles"] == {}
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.FOLLOWUP_TIMER).exists()

    def test_a_card_image_url_is_not_looked_up_in_the_library(self, tenancy, facade, monkeypatch):
        """A card's `image` holds an id *or* a URL; only an id is resolved."""
        from apps.flows.engine.nodes import send_message as module

        def _explode(media_id, workspace):
            raise AssertionError(f"resolve() should not be called for {media_id!r}")

        monkeypatch.setattr(module, "resolve", _explode)
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "card", "title": "Plans", "image": "https://example.test/{{first_name}}.png"}]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        _run(tenancy.workspace, document, connection=connection)

        assert _sent(facade).blocks[0].card.image_url == "https://example.test/Ada.png"

    def test_a_deleted_card_image_loses_the_picture_not_the_message(self, tenancy, facade, caplog):
        """A card image is decoration; unlike a media block it does not stop the send."""
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "card", "title": "Plans", "image": "0192f000-0000-7000-8000-0000000000ff"}]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        with caplog.at_level("WARNING"):
            _run(tenancy.workspace, document, connection=connection)

        assert _sent(facade).blocks[0].card.image_url == ""
        assert "card image" in caplog.text

    def test_no_buttons_means_it_continues(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        document = graph(
            [
                node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi"}]}),
                node("n", "action", _tagger("next"), x=200),
            ],
            [edge("m", "default", "n")],
        )

        contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"next"}

    def test_a_media_block_resolves_through_the_library(self, tenancy, facade, monkeypatch):
        from apps.flows.engine.nodes import send_message as module

        monkeypatch.setattr(
            module,
            "resolve",
            lambda media_id, workspace: {"url": f"https://cdn.test/{media_id}", "mime": "image/png", "kind": "image"},
        )
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "image", "media_id": "asset-1", "caption": "For {{first_name}}"}]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        _run(tenancy.workspace, document, connection=connection)

        block = _sent(facade).blocks[0]
        assert block.url == "https://cdn.test/asset-1"
        assert block.caption == "For Ada"

    def test_a_plain_url_media_block_is_passed_through(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "image", "url": "https://example.test/{{first_name}}.png"}]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        _run(tenancy.workspace, document, connection=connection)

        assert _sent(facade).blocks[0].url == "https://example.test/Ada.png"

    def test_a_deleted_asset_stops_the_message_not_the_flow(self, tenancy, facade, caplog):
        """``media_library.resolution``'s own contract, quoted in its docstring."""
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "text", "text": "Hi"}, {"type": "image", "media_id": "0192f000-0000-7000-8000-0000000000ff"}]
        document = graph(
            [node("m", "send_message", {"blocks": blocks}), node("n", "action", _tagger("next"), x=200)],
            [edge("m", "default", "n")],
        )

        with caplog.at_level("WARNING"):
            contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert facade.named("send_outbound") == []
        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"next"}
        assert "not sent" in caplog.text

    def test_a_failed_send_follows_default_onward(self, tenancy, facade):
        """SPEC §9.5: "sending failure does not kill the flow"."""
        facade.result = FakeMessage(status="failed", error="outside_window")
        connection = connection_for(tenancy.workspace)
        document = graph(
            [
                node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi"}]}),
                node("n", "action", _tagger("next"), x=200),
            ],
            [edge("m", "default", "n")],
        )

        contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"next"}

    def test_a_failed_send_does_not_wait_for_a_reply_nobody_saw(self, tenancy, facade):
        facade.result = FakeMessage(status="failed", error="blocked")
        connection = connection_for(tenancy.workspace)
        config = {
            "blocks": [{"type": "text", "text": "Pick:"}],
            "buttons": [{"id": "a", "label": "A", "action": "postback"}],
        }
        document = graph([node("m", "send_message", config)])

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED

    def test_a_run_with_no_channel_cannot_send(self, tenancy, facade, caplog):
        document = graph([node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi"}]})])

        with caplog.at_level("WARNING"):
            _contact, execution = _run(tenancy.workspace, document)

        assert facade.named("send_outbound") == []
        assert execution.status == ExecutionStatus.COMPLETED
        assert "no channel connection" in caplog.text

    def test_without_the_messaging_app_the_node_fails_by_name(self, tenancy, monkeypatch):
        """A deployment that cannot reach the facade fails by name, not by crash.

        Simulated rather than ambient: this was the real state of the tree until
        L3-A merged, and the behaviour still matters for a deployment that does
        not install messaging — but it has to be arranged now.
        """
        monkeypatch.setattr(messaging, "_services", lambda: None)
        connection = connection_for(tenancy.workspace)
        document = graph([node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi"}]})])

        _contact, execution = _run(tenancy.workspace, document, connection=connection)

        assert execution.status == ExecutionStatus.FAILED
        assert "ROADMAP contract 1" in execution.last_error

    def test_a_card_renders_its_four_fields(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        blocks = [
            {
                "type": "card",
                "title": "Hi {{first_name}}",
                "subtitle": "Our plans",
                "url_button": {"label": "See", "url": "https://example.test/"},
            }
        ]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        _run(tenancy.workspace, document, connection=connection)

        card = _sent(facade).blocks[0].card
        assert card.title == "Hi Ada"
        assert card.subtitle == "Our plans"
        assert card.buttons[0].url == "https://example.test/"

    def test_a_gallery_renders_every_card(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        blocks = [{"type": "gallery", "cards": [{"title": "One"}, {"title": "Two"}]}]
        document = graph([node("m", "send_message", {"blocks": blocks})])

        _run(tenancy.workspace, document, connection=connection)

        assert [card.title for card in _sent(facade).blocks[0].cards] == ["One", "Two"]

    def test_a_hostile_contact_name_is_inert_in_the_rendered_block(self, tenancy, facade):
        """SECURITY-BASELINE §3, end to end through a real node."""
        connection = connection_for(tenancy.workspace)
        contact = contact_for(tenancy.workspace, first_name="{{email}}", email="ada@example.test")
        document = graph([node("m", "send_message", {"blocks": [{"type": "text", "text": "Hi {{first_name}}"}]})])

        _run(tenancy.workspace, document, connection=connection, contact=contact)

        assert _sent(facade).blocks[0].text == "Hi {{email}}"


@pytest.mark.django_db
class TestSmartDelay:
    def _delay(self, workspace, config, *, contact=None):
        document = graph(
            [node("d", "smart_delay", config), node("n", "action", _tagger("later"), x=200)],
            [edge("d", "default", "n")],
        )
        return _run(workspace, document, contact=contact)

    def test_a_duration_schedules_and_parks(self, tenancy):
        before = timezone.now()
        _contact, execution = self._delay(
            tenancy.workspace, {"mode": "duration", "duration": {"value": 2, "unit": "hours"}}
        )

        assert execution.status == ExecutionStatus.WAITING_DELAY
        action = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.RESUME_EXECUTION)
        assert before + timedelta(hours=2) <= action.run_at <= timezone.now() + timedelta(hours=2)

    def test_the_scheduled_action_resumes_the_flow(self, tenancy):
        from apps.queueing.registry import get_handler

        contact, execution = self._delay(
            tenancy.workspace, {"mode": "duration", "duration": {"value": 1, "unit": "minutes"}}
        )
        action = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.RESUME_EXECUTION)

        get_handler(ActionType.RESUME_EXECUTION)(action.payload, action)

        execution.refresh_from_db()
        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"later"}

    def test_a_fixed_datetime_is_used_as_given(self, tenancy):
        moment = timezone.now() + timedelta(days=3)
        _contact, _execution = self._delay(
            tenancy.workspace, {"mode": "date", "date": {"datetime": moment.isoformat()}}
        )

        action = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.RESUME_EXECUTION)
        assert action.run_at == moment

    def test_a_date_field_on_the_contact_is_read_by_name(self, tenancy):
        contact = contact_for(tenancy.workspace)
        field = create_custom_field(tenancy.workspace, name="Renewal", field_type="date")
        set_field_value(contact, field, (timezone.now() + timedelta(days=10)).date())

        _contact, execution = self._delay(
            tenancy.workspace, {"mode": "date", "date": {"field": "renewal"}}, contact=contact
        )

        assert execution.status == ExecutionStatus.WAITING_DELAY

    def test_an_empty_date_field_fails_the_run(self, tenancy):
        """The alternative reading — "treat it as now" — sends to everyone missing it."""
        create_custom_field(tenancy.workspace, name="Renewal", field_type="date")

        _contact, execution = self._delay(tenancy.workspace, {"mode": "date", "date": {"field": "renewal"}})

        assert execution.status == ExecutionStatus.FAILED
        assert "nothing to compute a delay from" in execution.last_error

    def test_a_duration_with_nothing_to_count_fails_the_run(self, tenancy):
        document = graph([node("d", "smart_delay", {"mode": "duration"})])

        _contact, execution = _run_draft(tenancy.workspace, document)

        assert execution.status == ExecutionStatus.FAILED
        assert "nothing to compute a delay from" in execution.last_error

    def test_a_graph_with_no_mode_fails_the_run(self, tenancy):
        document = graph([node("d", "smart_delay", {})])

        _contact, execution = _run_draft(tenancy.workspace, document)

        assert execution.status == ExecutionStatus.FAILED
        assert "has no mode" in execution.last_error


@pytest.mark.django_db
class TestContinueWindow:
    """SPEC §11.5's sending window: forward only, into the next allowed slot."""

    def _run_at(self, workspace, window, *, contact=None, minutes=1):
        document = graph(
            [
                node(
                    "d",
                    "smart_delay",
                    {"mode": "duration", "duration": {"value": minutes, "unit": "minutes"}, "continue_window": window},
                )
            ]
        )
        _run(workspace, document, contact=contact)
        return ScheduledAction.objects.for_workspace(workspace).get(type=ActionType.RESUME_EXECUTION).run_at

    def test_a_moment_inside_the_window_is_untouched(self, tenancy):
        window = {"enabled": True, "days": list(WEEKDAYS), "from": "00:00", "to": "23:59"}
        run_at = self._run_at(tenancy.workspace, window)
        assert run_at <= timezone.now() + timedelta(minutes=2)

    def test_a_moment_outside_the_hours_waits_for_them(self, tenancy):
        """A window that has already closed today opens again tomorrow."""
        window = {"enabled": True, "from": "09:00", "to": "09:01"}
        run_at = self._run_at(tenancy.workspace, window)

        local = run_at.astimezone(ZoneInfo(tenancy.workspace.effective_timezone))
        assert local.hour == 9 and local.minute == 0

    def test_a_disallowed_day_is_skipped(self, tenancy):
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00"}
        run_at = self._run_at(tenancy.workspace, window)

        local = run_at.astimezone(ZoneInfo(tenancy.workspace.effective_timezone))
        assert local.weekday() == 0

    def test_the_contact_timezone_is_used_when_asked_for(self, tenancy):
        contact = contact_for(tenancy.workspace, timezone="Pacific/Auckland")
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00", "use_contact_timezone": True}

        run_at = self._run_at(tenancy.workspace, window, contact=contact)

        local = run_at.astimezone(ZoneInfo("Pacific/Auckland"))
        assert local.weekday() == 0
        assert local.hour == 9

    def test_an_unparseable_contact_timezone_falls_back(self, tenancy, caplog):
        """Contact timezones come from platform profiles — attacker-controlled."""
        contact = contact_for(tenancy.workspace, timezone="Mars/Olympus_Mons")
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00", "use_contact_timezone": True}

        with caplog.at_level("WARNING"):
            run_at = self._run_at(tenancy.workspace, window, contact=contact)

        assert run_at is not None
        assert "is not a timezone" in caplog.text

    def test_an_inverted_window_is_ignored_rather_than_obeyed(self, tenancy, caplog):
        """Honouring "17:00 to 09:00" literally would deliver never."""
        window = {"enabled": True, "from": "17:00", "to": "09:00"}

        with caplog.at_level("WARNING"):
            run_at = self._run_at(tenancy.workspace, window)

        assert run_at <= timezone.now() + timedelta(minutes=2)
        assert "not a usable range" in caplog.text

    def test_no_days_ticked_means_every_day(self, tenancy):
        window = {"enabled": True, "days": [], "from": "00:00", "to": "23:59"}
        run_at = self._run_at(tenancy.workspace, window)
        assert run_at <= timezone.now() + timedelta(minutes=2)

    def test_a_disabled_window_changes_nothing(self, tenancy):
        window = {"enabled": False, "days": ["mon"], "from": "09:00", "to": "09:01"}
        run_at = self._run_at(tenancy.workspace, window)
        assert run_at <= timezone.now() + timedelta(minutes=2)


@pytest.mark.django_db
class TestDataCollectionNode:
    def _flow(self, workspace, config):
        document = graph(
            [node("ask", "data_collection", config), node("done", "action", _tagger("answered"), x=200)],
            [edge("ask", "default", "done")],
        )
        return document

    def test_the_question_is_rendered_sent_and_then_it_waits(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        config = {
            "question": "What is your email, {{first_name}}?",
            "reply_type": "email",
            "target": {"type": "system_field", "key": "email"},
        }

        _contact, execution = _run(tenancy.workspace, self._flow(tenancy.workspace, config), connection=connection)

        assert _sent(facade).blocks[0].text == "What is your email, Ada?"
        assert execution.status == ExecutionStatus.WAITING_REPLY
        assert execution.wait_config["reply_type"] == "email"
        assert execution.wait_config["target"] == {"type": "system_field", "key": "email"}

    def test_a_failed_question_continues_rather_than_waiting_forever(self, tenancy, facade):
        facade.result = FakeMessage(status="failed", error="blocked")
        connection = connection_for(tenancy.workspace)
        config = {"question": "Email?", "reply_type": "email", "target": {"type": "system_field", "key": "email"}}

        contact, execution = _run(tenancy.workspace, self._flow(tenancy.workspace, config), connection=connection)

        assert execution.status == ExecutionStatus.COMPLETED
        assert {tag.name for tag in contact.tags.all()} == {"answered"}

    def test_an_unknown_reply_type_fails_the_run(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        config = {"question": "?", "reply_type": "postcode", "target": {"type": "system_field", "key": "email"}}

        _contact, execution = _run_draft(
            tenancy.workspace, graph([node("ask", "data_collection", config)]), connection=connection
        )

        assert execution.status == ExecutionStatus.FAILED
        assert "is not a reply type" in execution.last_error

    def test_the_timeout_block_arms_a_timer(self, tenancy, facade):
        connection = connection_for(tenancy.workspace)
        config = {
            "question": "Email?",
            "reply_type": "email",
            "target": {"type": "system_field", "key": "email"},
            "timeout": {"enabled": True, "delay": 1, "unit": "days"},
        }

        _run(tenancy.workspace, self._flow(tenancy.workspace, config), connection=connection)

        timer = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=ActionType.FOLLOWUP_TIMER)
        assert timer.run_at > timezone.now() + timedelta(hours=23)


class TestWindowArithmetic:
    """The window search on its own, where the clock can be pinned exactly."""

    def test_it_moves_forward_to_the_next_allowed_weekday(self):
        from apps.flows.engine.nodes.smart_delay import _into_window

        # A Saturday, 20:00 UTC.
        saturday = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00"}

        moved = _into_window(saturday, window, UTC)

        assert moved == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

    def test_a_moment_before_todays_window_waits_for_it(self):
        from apps.flows.engine.nodes.smart_delay import _into_window

        monday_dawn = datetime(2026, 8, 24, 6, 0, tzinfo=UTC)
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00"}

        assert _into_window(monday_dawn, window, UTC) == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)

    def test_a_moment_inside_the_window_is_returned_unchanged(self):
        from apps.flows.engine.nodes.smart_delay import _into_window

        monday_noon = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        window = {"enabled": True, "days": ["mon"], "from": "09:00", "to": "17:00"}

        assert _into_window(monday_noon, window, UTC) == monday_noon

    def test_the_window_is_read_in_the_given_clock(self):
        from apps.flows.engine.nodes.smart_delay import _into_window

        auckland = ZoneInfo("Pacific/Auckland")
        # 20:00 UTC Saturday is 08:00 Sunday in Auckland.
        saturday = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
        window = {"enabled": True, "days": ["sun"], "from": "09:00", "to": "17:00"}

        moved = _into_window(saturday, window, auckland)

        assert moved.astimezone(auckland).hour == 9
        assert moved.astimezone(auckland).weekday() == 6
