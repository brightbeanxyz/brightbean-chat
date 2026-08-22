"""The dispatch seam and the synthetic event id (ROADMAP contract 6)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from apps.channels import ingest
from apps.channels.events import EventType, NormalizedEvent

pytestmark = pytest.mark.django_db


def one_event(connection: Any) -> list[NormalizedEvent]:
    return [
        NormalizedEvent(
            type=EventType.MESSAGE,
            connection=connection,
            platform_user_id="u1",
            provider_event_id="e1",
            timestamp=datetime.now(UTC),
        )
    ]


class TestRegistration:
    def test_the_default_is_a_no_op(self, connection: Any) -> None:
        """The state issue #4 ships in: nothing registered, nothing happens."""
        assert ingest.registered_processors() == ()
        assert ingest.process_events(connection, one_event(connection)) is True

    def test_processors_run_in_registration_order(self, connection: Any) -> None:
        order: list[str] = []
        ingest.register_processor(lambda c, e: order.append("persist"), name="persist")
        ingest.register_processor(lambda c, e: order.append("route"), name="route")
        ingest.process_events(connection, one_event(connection))
        assert order == ["persist", "route"]

    def test_re_registering_a_name_replaces_rather_than_stacks(self, connection: Any) -> None:
        """A module imported twice must not double-process every event."""
        calls: list[str] = []
        ingest.register_processor(lambda c, e: calls.append("old"), name="persist")
        ingest.register_processor(lambda c, e: calls.append("new"), name="persist")
        ingest.process_events(connection, one_event(connection))
        assert calls == ["new"]

    def test_a_processor_can_be_removed(self, connection: Any) -> None:
        ingest.register_processor(lambda c, e: None, name="persist")
        ingest.unregister_processor("persist")
        assert ingest.registered_processors() == ()

    def test_removing_an_unknown_name_is_harmless(self) -> None:
        ingest.unregister_processor("never-registered")

    def test_a_processor_needs_a_name(self) -> None:
        with pytest.raises(ValueError, match="needs a name"):
            ingest.register_processor(lambda c, e: None, name="")


class TestIsolation:
    def test_a_failing_processor_does_not_stop_the_others(self, connection: Any) -> None:
        """ "Persistence failed" must not also mean "the STOP keyword was ignored"."""
        reached: list[str] = []

        def explode(conn: Any, events: Any) -> None:
            raise RuntimeError("boom")

        ingest.register_processor(explode, name="exploder")
        ingest.register_processor(lambda c, e: reached.append("ran"), name="after")

        assert ingest.process_events(connection, one_event(connection)) is False
        assert reached == ["ran"]

    def test_an_empty_batch_calls_nobody(self, connection: Any) -> None:
        calls: list[str] = []
        ingest.register_processor(lambda c, e: calls.append("x"), name="counter")
        assert ingest.process_events(connection, []) is True
        assert calls == []


class TestSyntheticEventId:
    def test_the_same_payload_gives_the_same_id(self) -> None:
        payload = {"from": "u1", "text": "hi", "ts": 1700000000}
        assert ingest.synthetic_event_id(payload) == ingest.synthetic_event_id(payload)

    def test_key_order_does_not_matter(self) -> None:
        assert ingest.synthetic_event_id({"a": 1, "b": 2}) == ingest.synthetic_event_id({"b": 2, "a": 1})

    def test_different_payloads_give_different_ids(self) -> None:
        assert ingest.synthetic_event_id({"text": "hi"}) != ingest.synthetic_event_id({"text": "ho"})

    def test_a_prefix_namespaces_the_id(self) -> None:
        assert ingest.synthetic_event_id({"a": 1}, prefix="sms:").startswith("sms:")

    def test_unserializable_values_do_not_raise(self) -> None:
        from datetime import UTC, datetime

        assert ingest.synthetic_event_id({"when": datetime(2026, 1, 1, tzinfo=UTC)})

    def test_the_id_fits_the_column(self) -> None:
        assert len(ingest.synthetic_event_id({"a": 1})) <= 200
