"""The contract-1 seam: what it does when ``apps.messaging`` cannot be reached.

Worth its own module because the answer is load-bearing. It was written while
L3-A was a parallel sibling, when every one of these functions was genuinely
unavailable and the fallback was simply what a reviewer running the suite saw.

L3-A has landed, so that is no longer the ambient state — but the fallback is
still live code. ``_services()`` answers ``None`` for a deployment that does not
install messaging, and for one where the app is installed but its services
module fails to import. Those paths decide whether a flow reaching a send node
reports why it stopped or crashes, so the tests now *simulate* the absence
rather than depending on it.
"""

import pytest

from apps.flows import messaging
from apps.flows.models import FlowExecution


@pytest.fixture
def messaging_unavailable(monkeypatch):
    """Make the seam answer "not installed", whatever this tree actually has."""
    monkeypatch.setattr(messaging, "_services", lambda: None)


@pytest.mark.usefixtures("messaging_unavailable")
class TestUnavailable:
    def test_available_is_false_when_the_facade_cannot_be_reached(self):
        assert messaging.available() is False

    @pytest.mark.parametrize(
        "call",
        [
            lambda: messaging.open_conversation(None),
            lambda: messaging.close_conversation(None),
            lambda: messaging.assign_conversation(None, None),
            lambda: messaging.pause_automation(None, None),
            lambda: messaging.upsert_contact_identity(None, "sms", "+1", source="x", opt_in=True),
        ],
    )
    def test_every_facade_call_names_the_missing_app(self, call):
        with pytest.raises(messaging.FacadeUnavailableError, match="ROADMAP contract 1"):
            call()

    def test_send_outbound_is_refused_by_name(self):
        with pytest.raises(messaging.FacadeUnavailableError, match="send_outbound"):
            messaging.send_outbound(
                workspace=None,
                contact=None,
                connection=None,
                outbound=None,
                source="automation",
                idempotency_key="k",
            )


class TestAvailable:
    def test_the_seam_resolves_now_that_l3a_has_landed(self):
        """The other half of the contract, and the reason the fixture exists."""
        assert messaging.available() is True


class TestFakingIt:
    def test_patching_the_seam_is_enough_to_make_a_call_work(self, monkeypatch):
        """One module to fake, which is the reason the seam exists at all."""
        recorded = []
        monkeypatch.setattr(messaging, "open_conversation", lambda *args: recorded.append(args))

        messaging.open_conversation("contact", "connection")

        assert recorded == [("contact", "connection")]


class TestIdempotencyKey:
    def test_it_is_spec_nine_fours_shape(self):
        """``exec:{execution_id}:node:{node_id}:attempt_bucket``.

        Both sides of contract 1 build on this: L3-B mints it, L3-A stores it
        under a unique index and skips the provider call on conflict.
        """
        execution = FlowExecution(pk="0192f000-0000-7000-8000-000000000001")

        assert messaging.message_idempotency_key(execution, "n1") == (
            "exec:0192f000-0000-7000-8000-000000000001:node:n1:0"
        )
        assert messaging.message_idempotency_key(execution, "n1", 3).endswith(":3")

    def test_a_retry_bucket_produces_a_different_key(self):
        """SPEC §9.4: a deliberate retry gets a fresh key; a re-run does not."""
        execution = FlowExecution(pk="0192f000-0000-7000-8000-000000000001")

        assert messaging.message_idempotency_key(execution, "n1", 0) != messaging.message_idempotency_key(
            execution, "n1", 1
        )
