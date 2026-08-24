"""SPEC §21 phase 1's reply budget, asserted as arithmetic rather than as a clock.

The measurement lives next door in ``test_first_reply_latency.py``. This module
is the half that still means something on a machine nobody benchmarked, and it
is the half that would have caught the regression a timing test catches late:
**the production constants have to fit inside the spec's ceiling**.

Three numbers decide whether a first reply can meet "p95 < 2 s":

* ``INLINE_BUDGET_SECONDS`` — how long routing will spend in the request before
  it gives up and hands the event to the queue. A reply that goes to the queue
  is no longer a *first* reply on the webhook's clock.
* ``CONNECT_TIMEOUT`` / ``READ_TIMEOUT`` — the hard stop on the outbound call
  the reply makes. SPEC §7.1 pairs them with the budget explicitly.
* The spec's own ceiling, which is read out of ``docs/SPEC.md`` rather than
  copied here, so the assertion tracks the document instead of drifting from it.

Nothing else in the suite ties those together. Raise ``INLINE_BUDGET_SECONDS``
to three seconds and every existing test still passes, while the criterion
becomes unmeetable by construction — the request would sit there past the
ceiling before the queue ever got a chance to take over.

What this module deliberately does **not** test, because it is covered:
``apps/flows/tests/test_routing_inline.py::TestTheGates`` already proves a slow
connection is flagged and then enqueues, and ``TestInlineExecution`` proves a
safe single-send flow completes inside the request.
"""

from __future__ import annotations

from apps.channels.providers.base import CONNECT_TIMEOUT, READ_TIMEOUT
from apps.flows.triggers.budget import COURTESY_BUDGET_SECONDS, INLINE_BUDGET_SECONDS
from tests.acceptance.criteria import spec_latency_budgets


class TestTheReplyStaysInsideTheSpecBudget:
    def test_the_inline_budget_leaves_room_under_the_spec_ceiling(self) -> None:
        """Routing must give up well before the reply is late, not exactly when it is.

        Equality would be the wrong assertion in both directions: a budget equal
        to the ceiling means the hand-off happens at the instant the reply has
        already missed it, and the queue still has to pick the event up
        afterwards.
        """
        ceiling = spec_latency_budgets()["first_reply"]
        assert ceiling > INLINE_BUDGET_SECONDS, (
            f"the inline routing budget is {INLINE_BUDGET_SECONDS}s against SPEC §21's {ceiling}s ceiling "
            f"for the first automated reply. Routing spends that budget inside the webhook request, so a "
            f"budget at or above the ceiling makes the criterion unmeetable however fast the code is."
        )

    def test_the_outbound_call_cannot_outlast_the_ceiling(self) -> None:
        """SPEC §7.1: "2 s hard timeout on the HTTP client" for the inline path.

        The adapter's timeouts are the last thing standing between a platform
        that has stopped answering and a webhook request that never returns.
        """
        ceiling = spec_latency_budgets()["first_reply"]
        assert ceiling >= CONNECT_TIMEOUT
        assert ceiling >= READ_TIMEOUT, (
            f"a read timeout of {READ_TIMEOUT}s exceeds SPEC §21's {ceiling}s first-reply ceiling: one "
            f"unresponsive platform would hold the reply past the budget with nothing to stop it."
        )

    def test_the_courtesy_calls_cannot_consume_the_whole_budget(self) -> None:
        """mark_seen and send_typing run first, and they are I/O too.

        They are worth their latency — they are what makes the wait feel
        answered — but they are spent before the reply is even attempted, so
        their budget has to be a fraction of the one they spend from.
        """
        assert COURTESY_BUDGET_SECONDS < INLINE_BUDGET_SECONDS, (
            f"the courtesy calls may spend {COURTESY_BUDGET_SECONDS}s of a {INLINE_BUDGET_SECONDS}s budget; "
            f"at or above it, greeting the contact would leave nothing for replying to them"
        )
