"""Reading a node out of an idempotency key, and refusing the ones that must not count.

The promise three modules make about ``FlowExecution.preview`` —
``apps/flows/models.py``, ``apps/flows/engine/runner.py`` and
``apps/broadcasts/handlers.py`` — is kept here and nowhere else, so this is the
file that would go red if it were ever quietly dropped.
"""

from typing import Any

import pytest

from apps.analytics.attribution import node_for, parse_idempotency_key
from apps.analytics.tests.conftest import ENTRY_NODE, make_execution
from apps.flows.messaging import message_idempotency_key

pytestmark = pytest.mark.django_db


class FakeMessage:
    """Just the two attributes attribution reads off a message row."""

    def __init__(self, workspace_id: Any, idempotency_key: str) -> None:
        self.workspace_id = workspace_id
        self.idempotency_key = idempotency_key


class TestParsing:
    def test_it_reads_spec_9_4s_key(self) -> None:
        parsed = parse_idempotency_key("exec:0192f0a0-0000-7000-8000-000000000001:node:welcome_1:0")

        assert parsed == ("0192f0a0-0000-7000-8000-000000000001", "welcome_1")

    def test_it_agrees_with_the_one_place_that_mints_the_key(self) -> None:
        """L3-B mints, L7-A reads. A key built from two different string literals
        is a counter that silently stops attributing."""

        class Execution:
            pk = "0192f0a0-0000-7000-8000-000000000002"

        key = message_idempotency_key(Execution(), "n7", 3)

        assert parse_idempotency_key(key) == ("0192f0a0-0000-7000-8000-000000000002", "n7")

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "in:some-provider-event-id",
            "agent:reply:1",
            "exec:not-a-uuid:node:n1:0",
            # No attempt bucket, so not SPEC §9.4's key.
            "exec:0192f0a0-0000-7000-8000-000000000001:node:n1",
            # A node id with a colon in it cannot exist — the graph schema's
            # ID_PATTERN forbids one — so this is a hand-written key, not a send.
            "exec:0192f0a0-0000-7000-8000-000000000001:node:a:b:0",
        ],
    )
    def test_anything_that_is_not_a_node_send_is_not_a_node_send(self, key: str) -> None:
        assert parse_idempotency_key(key) is None


class TestNodeFor:
    def test_it_resolves_the_flow_behind_a_real_execution(
        self, tenancy: Any, flow: Any, contact: Any, connection: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)
        message = FakeMessage(tenancy.workspace.pk, message_idempotency_key(execution, ENTRY_NODE))

        node = node_for(message)

        assert node is not None
        assert node.flow_id == flow.pk
        assert node.node_id == ENTRY_NODE

    def test_a_preview_run_attributes_nothing(self, tenancy: Any, flow: Any, contact: Any, connection: Any) -> None:
        """SPEC §16's "test on Telegram" is a real execution with real sends, and
        three modules promise its numbers stay out of these counters."""
        execution = make_execution(flow, contact, connection, preview=True)
        message = FakeMessage(tenancy.workspace.pk, message_idempotency_key(execution, ENTRY_NODE))

        assert node_for(message) is None

    def test_an_execution_from_another_workspace_attributes_nothing(
        self, tenancy: Any, other_tenancy: Any, flow: Any, contact: Any, connection: Any
    ) -> None:
        """The lookup is workspace-scoped, so a key naming somebody else's run
        cannot write a counter into their flow."""
        execution = make_execution(flow, contact, connection)
        message = FakeMessage(other_tenancy.workspace.pk, message_idempotency_key(execution, ENTRY_NODE))

        assert node_for(message) is None

    def test_a_vanished_execution_attributes_nothing(
        self, tenancy: Any, flow: Any, contact: Any, connection: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)
        key = message_idempotency_key(execution, ENTRY_NODE)
        execution.delete()

        assert node_for(FakeMessage(tenancy.workspace.pk, key)) is None

    def test_a_uuid_shaped_non_uuid_is_not_a_five_hundred(self, tenancy: Any) -> None:
        """36 characters of hex and dashes is UUID-shaped without being a UUID,
        and a UUIDField raises rather than not-matching for one."""
        message = FakeMessage(tenancy.workspace.pk, "exec:------------------------------------:node:n1:0")

        assert node_for(message) is None
