"""SPEC §21 phase 1's reply budget, asserted as arithmetic rather than as a clock.

The measurement lives next door in ``test_first_reply_latency.py``. This module
is the half that still means something on a machine nobody benchmarked, and it
is the half that would have caught the regression a timing test catches late:
**the production constants have to leave room under the spec's ceiling**.

Four constants decide whether a first reply can meet "p95 < 2 s":

* ``INLINE_BUDGET_SECONDS`` — how long routing will spend in the request before
  it gives up and hands the event to the queue. A reply that goes to the queue
  is no longer a *first* reply on the webhook's clock.
* ``READ_TIMEOUT`` — SPEC §7.1's "2 s hard timeout on the HTTP client", which
  ``base.py``'s own comment says is this constant.
* ``SLOW_CONNECTION_TTL_SECONDS`` — how long an overrunning connection is
  remembered. This is what bounds the *tail*, and the tests below explain why
  the timeouts do not: httpx applies connect and read to separate phases with no
  total deadline, so one pathological call can outlast the reply ceiling. That
  gap is real, and it is written up in this suite's README rather than papered
  over with an assertion that only looks like it bounds something.
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

from apps.channels.providers.base import BACKGROUND_TIMEOUT, CONNECT_TIMEOUT, READ_TIMEOUT
from apps.flows.triggers.budget import (
    COURTESY_BUDGET_SECONDS,
    INLINE_BUDGET_SECONDS,
    SLOW_CONNECTION_TTL_SECONDS,
)
from tests.acceptance.criteria import spec_latency_budgets

#: SPEC §7.1: "2 s hard timeout on the HTTP client". A different number from
#: §21's reply ceiling that happens to share its value — they are not the same
#: budget, and conflating them is how the composition below gets missed.
SPEC_HARD_HTTP_TIMEOUT = 2.0


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

    def test_the_read_timeout_is_the_hard_timeout_the_spec_names(self) -> None:
        """SPEC §7.1: "2 s hard timeout on the HTTP client" for the inline path.

        ``base.py``'s own comment says the read timeout is that number, so this
        pins the constant to the clause it implements.
        """
        assert SPEC_HARD_HTTP_TIMEOUT >= READ_TIMEOUT, (
            f"the read timeout is {READ_TIMEOUT}s; SPEC §7.1 budgets a {SPEC_HARD_HTTP_TIMEOUT}s hard "
            f"timeout for the inline path."
        )

    def test_the_inline_path_is_bounded_far_tighter_than_background_work(self) -> None:
        """The two phases compose, and that is worth stating rather than glossing.

        ``request_json`` builds ``httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)``,
        and httpx applies those to *separate phases* — there is no total
        deadline. So a platform that stalls the handshake and then stalls the
        read can hold one call for ``CONNECT_TIMEOUT + READ_TIMEOUT``, which is
        more than SPEC §21's ceiling for the whole reply.

        This test deliberately does **not** assert that sum against the ceiling,
        in either direction. Asserting it fits would fail today; asserting it
        does not fit would pin a deficiency and go red the day somebody adds a
        total deadline. What is true and worth holding is that the inline path
        is bounded, and bounded far below the background one.

        A single call outrunning the ceiling is survivable because the criterion
        is a **p95**, not a maximum: the first overrun flags the connection and
        every later event enqueues before doing any I/O, which is the test below.
        The composition itself is recorded as a follow-up in this suite's README.
        """
        worst_case = CONNECT_TIMEOUT + READ_TIMEOUT
        assert worst_case < BACKGROUND_TIMEOUT, (
            f"an inline call may take up to {worst_case}s across its two phases, against "
            f"{BACKGROUND_TIMEOUT}s for background work. The inline path has to be the tighter of the two."
        )

    def test_a_connection_that_overruns_stays_flagged_longer_than_the_budget(self) -> None:
        """What actually bounds the tail, given the timeouts above do not.

        Routing cannot predict a slow platform; it can decline to start a second
        one. The flag has to outlive the budget that set it, or a slow platform
        would be retried inline on the very next event and the p95 would be the
        timeout rather than the budget.
        """
        assert SLOW_CONNECTION_TTL_SECONDS > INLINE_BUDGET_SECONDS, (
            f"a slow connection is remembered for {SLOW_CONNECTION_TTL_SECONDS}s against a "
            f"{INLINE_BUDGET_SECONDS}s budget; at or below it the breaker would forget faster than it trips."
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
