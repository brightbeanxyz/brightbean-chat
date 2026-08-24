"""The §21 traceability gate: does the table still describe the codebase?

This is the thin driver over ``criteria.py``, in the shape ``tests/test_idor.py``
uses over ``tests/idor.py``. It needs no database and runs in milliseconds, which
is deliberate — a gate that is expensive to run is a gate people learn to skip.

The last four tests exist because ``apps/messaging/tests/test_write_sites.py``
put it best: a test that can only pass is not a test. If the resolver silently
accepted everything, every assertion above it would be decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.acceptance.criteria import (
    BLOCKING_ISSUES,
    CRITERIA,
    SECURITY_GATES,
    SECURITY_PHASE,
    CollectionRules,
    Criterion,
    UnjustifiedPendingError,
    UnresolvableVerificationError,
    Verification,
    _headings,
    all_rows,
    readme_ids,
    resolve,
    spec_clauses,
    unclaimed_clauses,
)


@pytest.fixture(scope="module")
def rules(pytestconfig: pytest.Config) -> CollectionRules:
    """Collection globs from pytest itself, not from three string literals here.

    If ``pyproject.toml`` ever changes how tests are collected, the resolver
    follows it instead of quietly disagreeing with the collector.
    """
    return CollectionRules.from_config(pytestconfig)


class TestTheTableDescribesRealTests:
    def test_every_target_resolves(self, rules: CollectionRules) -> None:
        """One test over every row, accumulating failures.

        Parametrising per row would turn a single renamed helper into twenty
        red tests saying the same thing; the IDOR sweep accumulates for the same
        reason.
        """
        failures: list[str] = []
        for row in all_rows():
            for target in row.targets:
                try:
                    resolve(target, rules)
                except UnresolvableVerificationError as exc:
                    failures.append(f"[{row.id}] {exc}")
        assert not failures, "\n\n".join(failures)

    def test_every_row_states_how_it_is_verified(self) -> None:
        for row in all_rows():
            assert row.note.strip(), f"{row.id} has no note; the prose is the part no assertion supplies"
            if row.verification is not Verification.PENDING:
                assert row.targets, f"{row.id} is {row.verification} with nothing to point at"

    def test_ids_are_unique(self) -> None:
        ids = [row.id for row in all_rows()]
        assert len(ids) == len(set(ids)), f"duplicate criterion ids: {sorted({i for i in ids if ids.count(i) > 1})}"


class TestTheTableCoversTheSpec:
    def test_every_clause_in_spec_21_is_claimed(self) -> None:
        """The assertion that makes this a gate rather than a curated list.

        A criterion nobody wrote a row for is invisible in a hand-maintained
        table. Here it is a failure naming the clause.
        """
        unclaimed = unclaimed_clauses()
        assert not unclaimed, (
            "SPEC §21 states acceptance criteria that no row in tests/acceptance/criteria.py owns:\n\n"
            + "\n".join(f"  phase {phase}: {clause}" for phase, clauses in unclaimed.items() for clause in clauses)
            + "\n\nAdd a Criterion for each — CI with a pytest target, MANUAL with a runbook anchor, or "
            "PENDING with a blocked_by issue listed in BLOCKING_ISSUES. There is no silent skip.\n"
            "Clauses parsed from docs/SPEC.md §21:\n"
            + "\n".join(
                f"  phase {phase}: {clause}" for phase, clauses in sorted(spec_clauses().items()) for clause in clauses
            )
        )

    def test_no_row_claims_a_clause_the_spec_does_not_state(self) -> None:
        """The other direction: a paraphrased clause would silently stop matching."""
        stated = {(phase, clause) for phase, clauses in spec_clauses().items() for clause in clauses}
        invented = [(row.id, row.phase, row.clause) for row in CRITERIA if (row.phase, row.clause) not in stated]
        assert not invented, (
            "these rows quote a clause SPEC §21 does not state — copy it verbatim, or the completeness "
            f"check above silently stops covering it: {invented}"
        )

    def test_all_three_phases_were_parsed(self) -> None:
        """A parser that found nothing would make every check above vacuous."""
        clauses = spec_clauses()
        assert sorted(clauses) == [1, 2, 3], f"expected three phases, parsed {sorted(clauses)}"
        assert all(clauses[phase] for phase in (1, 2, 3)), f"a phase parsed to no clauses at all: {clauses}"
        assert "STOP suppresses within one inbound event" in clauses[2], (
            f"§21's punctuation changed shape and the split no longer lands on clauses: {clauses[2]}"
        )


class TestPendingRowsAreJustified:
    def test_each_pending_row_names_an_explained_issue(self) -> None:
        for row in all_rows():
            if row.verification is Verification.PENDING:
                assert row.blocked_by in BLOCKING_ISSUES, f"{row.id} is blocked on an unexplained issue"
                assert len(BLOCKING_ISSUES[row.blocked_by]) > 100, (
                    f"BLOCKING_ISSUES[{row.blocked_by}] is too short to be a reason. Say who owns the "
                    f"criterion, why this suite is not writing the test, and what flipping the row needs."
                )

    def test_no_blocking_issue_is_listed_without_a_row_using_it(self) -> None:
        """A stale waiver reads as a live one. Delete it with the row it excused."""
        used = {row.blocked_by for row in all_rows() if row.blocked_by is not None}
        assert set(BLOCKING_ISSUES) == used, (
            f"BLOCKING_ISSUES lists {sorted(set(BLOCKING_ISSUES) - used)} which no row is blocked on. "
            f"When a blocker lands and its row flips to CI, its entry goes too."
        )


class TestTheReadmeAndTheRegistryAgree:
    def test_every_row_appears_in_the_readme(self) -> None:
        documented = readme_ids()
        missing = sorted(row.id for row in all_rows() if row.id not in documented)
        assert not missing, f"tests/acceptance/README.md does not mention {missing}"

    def test_the_readme_invents_no_criteria(self) -> None:
        known = {row.id for row in all_rows()}
        invented = sorted(readme_ids() - known)
        assert not invented, f"tests/acceptance/README.md mentions {invented}, which criteria.py does not define"


class TestTheResolverActuallyFires:
    """Prove the detector detects. Every assertion above rests on this class."""

    def test_it_rejects_a_class_that_does_not_exist(self, rules: CollectionRules) -> None:
        with pytest.raises(UnresolvableVerificationError, match="defines no class"):
            resolve("apps/flows/tests/test_locking.py::TestNoSuchClassHere", rules)

    def test_it_rejects_a_method_that_does_not_exist(self, rules: CollectionRules) -> None:
        with pytest.raises(UnresolvableVerificationError, match="no test method"):
            resolve("apps/flows/tests/test_locking.py::TestOneStepPerContact::test_nope", rules)

    def test_it_rejects_a_parametrized_id(self, rules: CollectionRules) -> None:
        with pytest.raises(UnresolvableVerificationError, match="parametrized ids churn"):
            resolve("apps/messaging/tests/test_compliance.py::TestOutsideTheWindow::test_x[telegram]", rules)

    def test_it_rejects_a_file_pytest_would_not_collect(self, rules: CollectionRules) -> None:
        with pytest.raises(UnresolvableVerificationError, match="does not collect"):
            resolve("apps/flows/models.py::Flow", rules)

    def test_it_rejects_a_missing_runbook_anchor(self, rules: CollectionRules) -> None:
        """Against the real README, so this cannot pass by failing earlier.

        An earlier version of this test built a file in tmp_path, which resolves
        outside the repo — so it raised "no such file" and never exercised the
        anchor branch at all. A detector self-test that fires for the wrong
        reason is the same problem it exists to rule out.
        """
        with pytest.raises(UnresolvableVerificationError, match="no heading anchored"):
            resolve("tests/acceptance/README.md#no-such-anchor", rules)

    def test_it_accepts_an_anchor_that_is_really_there(self, rules: CollectionRules) -> None:
        resolve("tests/acceptance/README.md#manual-runbooks", rules)

    def test_it_rejects_a_node_id_deeper_than_pytest_goes(self) -> None:
        """Extra segments used to be dropped silently, so a typo resolved clean."""
        with pytest.raises(UnresolvableVerificationError, match="at most three deep"):
            resolve(
                "apps/flows/tests/test_locking.py::TestOneStepPerContact::test_x::nested",
                CollectionRules(files=("test_*.py",), classes=("Test*",), functions=("test_*",)),
            )

    def test_it_ignores_a_hash_inside_a_fenced_code_block(self, tmp_path: Path) -> None:
        """A shell comment in a ```bash fence is not a heading, and must not mint an anchor."""
        book = tmp_path / "runbook.md"
        book.write_text("# Real Heading\n\n```bash\n# not a heading\n```\n")
        assert _headings(book.read_text()) == {"real-heading"}

    def test_a_pending_row_without_an_explained_blocker_refuses_to_exist(self) -> None:
        with pytest.raises(UnjustifiedPendingError, match="not in BLOCKING_ISSUES"):
            Criterion(
                id="p3-invented",
                phase=3,
                clause="whatever",
                verification=Verification.PENDING,
                blocked_by=99999,
                note="x",
            )


class TestARowCannotClaimMoreThanItsTargets:
    """The overstatement the p95 and 10k rows were split to stop, pinned.

    ``resolve()`` dispatches on whether a target contains ``#``, not on what the
    row says it is — so without these, a CI row could point at a heading in the
    README, resolve perfectly, and have the table report it as verified in CI
    with nothing but prose behind it.
    """

    def test_a_ci_row_cannot_point_at_a_runbook(self) -> None:
        with pytest.raises(ValueError, match="which is a runbook anchor"):
            Criterion(
                id="p3-prose",
                phase=3,
                clause="whatever",
                verification=Verification.CI,
                targets=("tests/acceptance/README.md#manual-runbooks",),
                note="claims CI, points at prose",
            )

    def test_a_ci_row_cannot_point_at_something_that_is_not_a_module(self) -> None:
        with pytest.raises(ValueError, match="not a Python module"):
            Criterion(
                id="p3-notamodule",
                phase=3,
                clause="whatever",
                verification=Verification.CI,
                targets=("apps/flows/tests/",),
                note="claims CI, points at a directory",
            )

    def test_a_manual_row_cannot_point_at_a_test(self) -> None:
        """The mirror. A manual row backed by a test is a runbook nobody wrote."""
        with pytest.raises(ValueError, match="not a runbook anchor"):
            Criterion(
                id="p3-notarunbook",
                phase=3,
                clause="whatever",
                verification=Verification.MANUAL,
                targets=("apps/flows/tests/test_locking.py::TestOneStepPerContact",),
                note="claims manual, points at a test",
            )

    def test_a_pending_row_cannot_name_a_target_at_all(self) -> None:
        with pytest.raises(UnjustifiedPendingError, match="cannot name a target"):
            Criterion(
                id="p3-both",
                phase=3,
                clause="whatever",
                verification=Verification.PENDING,
                blocked_by=27,
                targets=("apps/flows/tests/test_locking.py",),
                note="pending and pointing somewhere",
            )

    def test_a_pending_row_with_no_blocker_at_all_refuses_to_exist(self) -> None:
        """The branch the single earlier self-test left unpinned."""
        with pytest.raises(UnjustifiedPendingError, match="no blocked_by"):
            Criterion(
                id="p3-ownerless",
                phase=3,
                clause="whatever",
                verification=Verification.PENDING,
                note="pending, owned by nobody",
            )


class TestTheGateIsWorthReading:
    def test_the_security_gates_are_separate_from_the_spec_clauses(self) -> None:
        """They are checked, but they are not §21 and must not pad its coverage."""
        assert all(row.phase == SECURITY_PHASE for row in SECURITY_GATES)
        assert all(row.phase in (1, 2, 3) for row in CRITERIA)
