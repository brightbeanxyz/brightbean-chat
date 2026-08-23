"""The ordered hook registry — ROADMAP contract 6's deliverable.

No database and no models: that is the property the registry is built for, so
L5-D and L6-C can register from their own ``ready()`` without the import order of
``INSTALLED_APPS`` mattering.
"""

import pytest

from apps.flows.triggers.hooks import (
    ENGINE_STAGES,
    RUNS_WHILE_PAUSED,
    STAGE_ORDER,
    Consumed,
    Deferred,
    DuplicateHookError,
    HookAbortedError,
    Passed,
    Stage,
    hook_names,
    register_hook,
    registered_hooks,
    run_stage,
    stages_from,
    unregister_hook,
)


@pytest.fixture
def probe():
    """A recorder that names itself, so dispatch order is observable."""
    calls: list[str] = []

    def make(name, outcome=None):
        def hook(context):
            calls.append(name)
            return outcome

        return hook

    make.calls = calls
    return make


class TestTheStages:
    def test_the_order_is_contract_sixes(self):
        assert STAGE_ORDER == (
            Stage.HARD_OPTOUT,
            Stage.POST_PERSIST,
            Stage.RESUME,
            Stage.TRIGGER,
            Stage.DEFAULT_REPLY,
        )

    def test_pause_and_engine_sets_partition_the_stages(self):
        """Every stage either survives a takeover or is automation. Nothing is
        both, and nothing is neither."""
        assert set(STAGE_ORDER) == RUNS_WHILE_PAUSED | ENGINE_STAGES
        assert not RUNS_WHILE_PAUSED & ENGINE_STAGES

    def test_stages_from_slices_the_tail(self):
        assert stages_from(Stage.RESUME) == (Stage.RESUME, Stage.TRIGGER, Stage.DEFAULT_REPLY)
        assert stages_from("hard_optout") == STAGE_ORDER

    def test_an_unknown_stage_raises_at_registration(self):
        """Not at dispatch: a hook registered into nowhere would simply never run."""
        with pytest.raises(LookupError):
            register_hook(lambda context: None, stage="somewhere", name="nope")


class TestOrdering:
    def test_dispatch_order_does_not_depend_on_registration_order(self, probe):
        """INSTALLED_APPS order is not a contract anybody should have to know."""
        register_hook(probe("c"), stage=Stage.TRIGGER, name="c", priority=30)
        register_hook(probe("a"), stage=Stage.TRIGGER, name="a", priority=10)
        register_hook(probe("b"), stage=Stage.TRIGGER, name="b", priority=20)
        forwards = hook_names()

        for name in ("a", "b", "c"):
            unregister_hook(name)
        register_hook(probe("a"), stage=Stage.TRIGGER, name="a", priority=10)
        register_hook(probe("b"), stage=Stage.TRIGGER, name="b", priority=20)
        register_hook(probe("c"), stage=Stage.TRIGGER, name="c", priority=30)

        assert hook_names() == forwards

    def test_equal_priorities_break_on_name(self, probe):
        register_hook(probe("z"), stage=Stage.POST_PERSIST, name="z")
        register_hook(probe("m"), stage=Stage.POST_PERSIST, name="m")

        assert [item.name for item in registered_hooks(Stage.POST_PERSIST)] == ["m", "z"]

    def test_hooks_are_grouped_by_stage_in_stage_order(self, probe):
        register_hook(probe("late"), stage=Stage.DEFAULT_REPLY, name="late", priority=1)
        register_hook(probe("early"), stage=Stage.HARD_OPTOUT, name="early", priority=99)

        names = hook_names()
        assert names.index("early") < names.index("late")

    def test_a_snapshot_is_immune_to_registration_during_dispatch(self, probe):
        def registers_another(context):
            register_hook(probe("sneaked"), stage=Stage.TRIGGER, name="sneaked", priority=1)
            return None

        register_hook(registers_another, stage=Stage.TRIGGER, name="registrar")
        run_stage(Stage.TRIGGER, object())

        assert "sneaked" not in probe.calls


class TestRegistration:
    def test_a_duplicate_name_raises(self, probe):
        register_hook(probe("one"), stage=Stage.TRIGGER, name="shared")
        with pytest.raises(DuplicateHookError):
            register_hook(probe("two"), stage=Stage.RESUME, name="shared")

    def test_replace_swaps_in_place(self, probe):
        register_hook(probe("one"), stage=Stage.TRIGGER, name="shared")
        register_hook(probe("two"), stage=Stage.TRIGGER, name="shared", replace_existing=True)

        run_stage(Stage.TRIGGER, object())
        assert probe.calls == ["two"]

    def test_a_name_is_required(self, probe):
        with pytest.raises(ValueError):
            register_hook(probe("x"), stage=Stage.TRIGGER, name="")

    def test_unregistering_an_unknown_name_is_a_no_op(self):
        unregister_hook("never-registered")


class TestOutcomes:
    def test_none_means_passed(self, probe):
        register_hook(probe("a"), stage=Stage.TRIGGER, name="a", priority=1)
        register_hook(probe("b"), stage=Stage.TRIGGER, name="b", priority=2)

        assert isinstance(run_stage(Stage.TRIGGER, object()), Passed)
        assert probe.calls == ["a", "b"]

    def test_consumed_stops_the_stage(self, probe):
        register_hook(probe("a", Consumed("mine")), stage=Stage.TRIGGER, name="a", priority=1)
        register_hook(probe("b"), stage=Stage.TRIGGER, name="b", priority=2)

        outcome = run_stage(Stage.TRIGGER, object())
        assert isinstance(outcome, Consumed)
        assert probe.calls == ["a"]

    def test_the_runner_stamps_the_consumer(self, probe):
        """A hook naming somebody else would make the log a work of fiction."""
        register_hook(probe("a", Consumed("mine", by="someone-else")), stage=Stage.TRIGGER, name="a")

        assert run_stage(Stage.TRIGGER, object()).by == "a"

    def test_deferred_stops_the_stage_too(self, probe):
        register_hook(probe("a", Deferred("later")), stage=Stage.TRIGGER, name="a", priority=1)
        register_hook(probe("b"), stage=Stage.TRIGGER, name="b", priority=2)

        assert isinstance(run_stage(Stage.TRIGGER, object()), Deferred)
        assert probe.calls == ["a"]


class TestErrorIsolation:
    def test_a_raising_hook_elsewhere_is_treated_as_passed(self, probe):
        def explodes(context):
            raise RuntimeError("boom")

        register_hook(explodes, stage=Stage.POST_PERSIST, name="explodes", priority=1)
        register_hook(probe("after"), stage=Stage.POST_PERSIST, name="after", priority=2)

        assert isinstance(run_stage(Stage.POST_PERSIST, object()), Passed)
        assert probe.calls == ["after"]

    def test_a_raising_hard_optout_hook_aborts_the_event(self, probe):
        """Fail closed: if we cannot establish that this contact has *not* opted
        out, nothing downstream may send them anything."""

        def explodes(context):
            raise RuntimeError("boom")

        register_hook(explodes, stage=Stage.HARD_OPTOUT, name="explodes", priority=1)
        register_hook(probe("after"), stage=Stage.HARD_OPTOUT, name="after", priority=2)

        with pytest.raises(HookAbortedError):
            run_stage(Stage.HARD_OPTOUT, object())
        assert probe.calls == []


class TestTheBuiltIns:
    def test_post_persist_ships_empty_and_named(self):
        """L6-C's slot. The stage existing and being dispatched now is what makes
        inbox rules a registration rather than an edit to routing code."""
        from apps.flows.triggers.stages import register_builtin_hooks

        register_builtin_hooks()
        assert registered_hooks(Stage.POST_PERSIST) == ()
        assert [item.name for item in registered_hooks(Stage.HARD_OPTOUT)] == ["opt_out_event"]
        assert [item.name for item in registered_hooks(Stage.RESUME)] == ["waiting_execution"]
        assert [item.name for item in registered_hooks(Stage.TRIGGER)] == ["trigger_match"]
        assert [item.name for item in registered_hooks(Stage.DEFAULT_REPLY)] == ["default_reply"]
