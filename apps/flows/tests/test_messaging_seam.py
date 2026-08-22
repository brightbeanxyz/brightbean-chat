"""The contract-1 seam: what it does while ``apps.messaging`` is not installed.

Worth its own module because the answer is load-bearing for the whole PR. L3-A
is a parallel sibling, so on this branch every one of these functions is
unavailable, and the engine's behaviour in that state is what a reviewer running
the suite actually sees. Once #8 merges the seam resolves for real and these
tests describe the fallback rather than the norm.
"""

import pytest

from apps.flows import messaging
from apps.flows.models import FlowExecution


class TestUnavailable:
    def test_available_is_false_before_l3a_lands(self):
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
