"""When a step runs: the delay, the window, and which clock reads it.

SPEC §12 gives one sentence — "each step waits its delay from the previous
step's send, adjusted to the step's send window" — and every acceptance
criterion in issue #22 that is not about the queue is about that sentence.

The window arithmetic itself lives in :mod:`apps.common.windows` and is shared
with the ``smart_delay`` node; what is under test here is the composition:
delay, then window, in the right clock, from the right base.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from apps.campaigns.models import DelayUnit
from apps.campaigns.scheduling import next_run_for
from apps.campaigns.tests.support import contact_for, sequence_with, step_for
from apps.common.windows import clock_for, into_window

BUSINESS_HOURS = {
    "enabled": True,
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "from": "09:00",
    "to": "17:00",
    "use_contact_timezone": True,
}


@pytest.mark.django_db
class TestTheDelay:
    def test_a_step_with_no_window_runs_exactly_its_delay_later(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=1, delay_value=3, delay_unit=DelayUnit.HOURS)
        step = sequence.steps.get()
        base = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

        run_at = next_run_for(step, base=base, contact=contact_for(tenancy.workspace), workspace=tenancy.workspace)

        assert run_at == base + timedelta(hours=3)

    def test_a_zero_delay_runs_immediately(self, tenancy):
        """A first step with no wait is a legitimate campaign: "welcome, now"."""
        sequence = sequence_with(tenancy.workspace, steps=1, delay_value=0, delay_unit=DelayUnit.MINUTES)
        base = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

        run_at = next_run_for(
            sequence.steps.get(), base=base, contact=contact_for(tenancy.workspace), workspace=tenancy.workspace
        )

        assert run_at == base

    def test_worker_lag_does_not_compress_the_next_gap(self, tenancy):
        """The acceptance criterion, stated directly.

        Step 2 is computed from when step 1 *ran*, not from when it was due, so
        a worker forty minutes behind delays the campaign rather than shortening
        every gap in it.
        """
        sequence = sequence_with(tenancy.workspace, steps=2, delay_value=1, delay_unit=DelayUnit.DAYS)
        second = sequence.steps.get(position=2)
        contact = contact_for(tenancy.workspace)
        due = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
        actually_ran = due + timedelta(minutes=40)

        run_at = next_run_for(second, base=actually_ran, contact=contact, workspace=tenancy.workspace)

        assert run_at - actually_ran == timedelta(days=1)
        assert run_at - due > timedelta(days=1)


@pytest.mark.django_db
class TestTheSendWindow:
    """Mon–Fri 09:00–17:00 in the contact's timezone, across DST and without one."""

    def _run_at(self, tenancy, base, *, timezone_name="", name="Onboarding"):
        sequence = sequence_with(
            tenancy.workspace,
            steps=1,
            name=name,
            delay_value=0,
            delay_unit=DelayUnit.MINUTES,
            send_window=BUSINESS_HOURS,
        )
        contact = contact_for(tenancy.workspace, timezone=timezone_name)
        return next_run_for(sequence.steps.get(), base=base, contact=contact, workspace=tenancy.workspace)

    def test_a_weekend_moment_waits_for_monday_morning(self, tenancy):
        # Saturday 20:00 UTC is Saturday 21:00 in Berlin.
        saturday = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)

        run_at = self._run_at(tenancy, saturday, timezone_name="Europe/Berlin")

        berlin = ZoneInfo("Europe/Berlin")
        assert run_at.astimezone(berlin) == datetime(2026, 8, 24, 9, 0, tzinfo=berlin)

    def test_a_moment_inside_the_window_is_not_moved(self, tenancy):
        tuesday_noon = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)  # 12:00 in Berlin

        assert self._run_at(tenancy, tuesday_noon, timezone_name="Europe/Berlin") == tuesday_noon

    @pytest.mark.parametrize(
        ("base", "expected_offset_hours"),
        [
            # New York, the Friday before the spring-forward Sunday: the window
            # opens at 09:00 EST (UTC-5) …
            (datetime(2026, 3, 6, 3, 0, tzinfo=UTC), 5),
            # … and the Monday after it opens at 09:00 EDT (UTC-4). Same wall
            # clock, different instant — which is the whole point of resolving
            # the window per local day rather than per instant.
            (datetime(2026, 3, 9, 3, 0, tzinfo=UTC), 4),
        ],
    )
    def test_the_window_keeps_its_wall_clock_across_dst(self, tenancy, base, expected_offset_hours):
        run_at = self._run_at(tenancy, base, timezone_name="America/New_York")

        assert run_at == base.replace(hour=9 + expected_offset_hours, minute=0)

    def test_a_contact_with_no_timezone_falls_back_to_the_workspace(self, tenancy):
        """A contact timezone is a free-text column fed from platform profiles.

        Blank and unparseable both have to fall back, or a campaign would stall
        for everybody whose profile the platform did not report.
        """
        saturday = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)

        blank = self._run_at(tenancy, saturday, timezone_name="", name="Blank")
        rubbish = self._run_at(tenancy, saturday, timezone_name="Mars/Olympus_Mons", name="Rubbish")

        assert blank == rubbish
        # The workspace has no timezone of its own in the fixture, so this is
        # settings.TIME_ZONE — the point is that both land on the same Monday.
        assert blank.astimezone(UTC).weekday() == 0

    def test_an_inverted_window_delays_rather_than_never_fires(self, tenancy):
        """The builder can save one, and "never" is the worse failure."""
        sequence = sequence_with(
            tenancy.workspace,
            steps=1,
            delay_value=1,
            delay_unit=DelayUnit.HOURS,
            send_window={"enabled": True, "days": ["mon"], "from": "17:00", "to": "09:00"},
        )
        base = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)

        run_at = next_run_for(
            sequence.steps.get(), base=base, contact=contact_for(tenancy.workspace), workspace=tenancy.workspace
        )

        assert run_at == base + timedelta(hours=1)


class TestTheSharedWindowHelpers:
    """``apps.common.windows`` on its own, where the clock can be pinned exactly.

    Its other caller is the ``smart_delay`` node, whose own tests cover the same
    search from the other side — these are the properties a sequence step relies
    on that a delay node does not exercise.
    """

    def test_no_days_ticked_means_every_day(self):
        window = {"enabled": True, "days": [], "from": "09:00", "to": "17:00"}
        saturday_dawn = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)

        assert into_window(saturday_dawn, window, UTC) == datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

    def test_a_disabled_window_is_not_consulted(self):
        moment = datetime(2026, 8, 22, 3, 0, tzinfo=UTC)

        assert into_window(moment, {"enabled": False, "days": ["mon"], "from": "09:00", "to": "17:00"}, UTC) == moment

    def test_a_missing_window_is_not_consulted(self):
        moment = datetime(2026, 8, 22, 3, 0, tzinfo=UTC)

        assert into_window(moment, None, UTC) == moment

    def test_clock_for_prefers_the_contact_only_when_asked(self):
        class Row:
            timezone = "Pacific/Auckland"
            effective_timezone = "Europe/Berlin"

        assert str(clock_for(Row(), Row(), use_contact_timezone=True)) == "Pacific/Auckland"
        assert str(clock_for(Row(), Row(), use_contact_timezone=False)) == "Europe/Berlin"


@pytest.mark.django_db
class TestStepWindowDefaults:
    def test_a_partial_stored_window_reads_with_the_defaults_filled_in(self, tenancy):
        sequence = sequence_with(tenancy.workspace, steps=0)
        step = step_for(sequence, position=1, send_window={"enabled": True})

        assert step.window["from"] == "09:00"
        assert step.window["days"] == []
        assert step.window["use_contact_timezone"] is False
