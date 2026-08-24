"""``docs/security-audit.md`` is checked, not just written.

Issue #29's deliverable is a map from every SECURITY-BASELINE bullet to the test
that enforces it. A map is only worth having if it is true, and this one has two
ways of going quietly false: a bullet added to the baseline that nobody maps,
and a test renamed out from under a row that still cites it. Both leave the
document *looking* complete, which is worse than not having it — it is the
artefact a reviewer or a self-hoster reads **instead of** the code.

So the ids are compared as sets, in both directions, the way
``apps/api/tests/test_isolation.py`` compares waived routes against swept ones;
and every cited test id is resolved against the source. Resolution is static —
``ast.parse`` and a walk, rather than ``pytest --collect-only`` — because it has
to be deterministic under ``-n auto`` and must not import an app to answer.

**What this cannot prove**: that a named test *passes*. That is the `test` CI
job's business. This proves the map is honest; the suite proves the map's
destinations are green. Neither alone is the claim.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "SECURITY-BASELINE.md"
AUDIT = ROOT / "docs" / "security-audit.md"
SPEC = ROOT / "docs" / "SPEC.md"

#: The only statuses a row may carry. A fifth would be a way to say "fine" with
#: nothing behind it, which is the thing this document exists to prevent.
STATUSES = frozenset({"COVERED", "PARTIAL", "DEVIATION", "NOT YET BUILT"})

#: Statuses that must carry an explanation. `COVERED` needs none; the tests are
#: the explanation.
MUST_EXPLAIN = frozenset({"PARTIAL", "DEVIATION", "NOT YET BUILT"})


def baseline_ids() -> set[str]:
    """``{"§1.1", ...}`` — the N-th bullet under the M-th numbered heading."""
    ids: set[str] = set()
    section = 0
    index = 0
    for line in BASELINE.read_text().splitlines():
        heading = re.match(r"^## (\d+)\. ", line)
        if heading:
            section, index = int(heading.group(1)), 0
            continue
        if section and line.startswith("- "):
            index += 1
            ids.add(f"§{section}.{index}")
    return ids


def spec_19_ids() -> set[str]:
    """SPEC §19's bullets, numbered the same way."""
    lines = SPEC.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## 19. "))
    count = 0
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            count += 1
    return {f"SPEC §19.{n}" for n in range(1, count + 1)}


def audit_rows() -> list[tuple[str, str, str]]:
    """``(id, tests cell, status cell)`` for every mapped row."""
    rows = []
    for line in AUDIT.read_text().splitlines():
        if not line.startswith("| §") and not line.startswith("| SPEC §"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        rows.append((cells[0], cells[2], cells[3]))
    return rows


def cited_test_ids() -> set[str]:
    """Every backticked ``path`` or ``path::Class`` in a Tests cell."""
    found: set[str] = set()
    for _, tests, _ in audit_rows():
        for token in re.findall(r"`([^`]+)`", tests):
            if token.startswith(("apps/", "tests/")) and token.endswith((".py",)) or "::" in token:
                found.add(token)
    return found


def resolves(test_id: str) -> bool:
    """Does ``path::Class`` (or ``path``) name something that exists?"""
    path_part, _, member = test_id.partition("::")
    path = ROOT / path_part
    if not path.exists():
        return False
    if not member:
        return True
    tree = ast.parse(path.read_text())
    wanted, _, method = member.partition("::")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.name == wanted:
            if not method:
                return True
            return any(
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and child.name == method
                for child in ast.walk(node)
            )
    return False


class TestTheAuditIsComplete:
    def test_it_covers_exactly_the_baselines_bullets(self) -> None:
        """Both directions. A bullet with no row is an unenforced requirement
        with nothing saying so; a row with no bullet is a map to nowhere."""
        mapped = {row_id for row_id, _, _ in audit_rows() if not row_id.startswith("SPEC")}

        assert mapped == baseline_ids(), (
            f"docs/security-audit.md and docs/SECURITY-BASELINE.md disagree: "
            f"{sorted(mapped ^ baseline_ids())}. Every baseline bullet needs a row naming the test "
            f"that enforces it."
        )

    def test_it_covers_every_spec_19_bullet(self) -> None:
        mapped = {row_id for row_id, _, _ in audit_rows() if row_id.startswith("SPEC")}

        assert mapped == spec_19_ids(), f"SPEC §19 coverage disagrees: {sorted(mapped ^ spec_19_ids())}"

    def test_every_row_names_at_least_one_test(self) -> None:
        bare = [row_id for row_id, tests, _ in audit_rows() if "`" not in tests]

        assert not bare, f"These rows cite no test at all: {bare}"


class TestEveryCitationResolves:
    @pytest.mark.parametrize("test_id", sorted(cited_test_ids()))
    def test_the_named_test_exists(self, test_id: str) -> None:
        """A renamed class silently turns a row into a lie. This is what stops
        that being invisible."""
        assert resolves(test_id), (
            f"docs/security-audit.md cites {test_id}, which does not resolve. Either the test was "
            f"renamed — update the row — or it was deleted, in which case that baseline item no "
            f"longer has a linked test and the row's status has to change."
        )


class TestTheStatusVocabularyIsClosed:
    def test_every_status_is_one_of_four(self) -> None:
        offenders = []
        for row_id, _, status in audit_rows():
            word = status.split("—")[0].strip()
            if word not in STATUSES:
                offenders.append(f"{row_id}: {word!r}")

        assert not offenders, f"Unknown status: {offenders}. The vocabulary is {sorted(STATUSES)}."

    def test_anything_short_of_covered_explains_itself(self) -> None:
        """The mechanism behind "zero baseline items without a linked passing
        test": an item cannot be given a non-COVERED status without saying why
        in the same cell, so a gap cannot be recorded as a shrug."""
        offenders = []
        for row_id, _, status in audit_rows():
            word = status.split("—")[0].strip()
            if word in MUST_EXPLAIN and "—" not in status:
                offenders.append(row_id)

        assert not offenders, f"These rows are not COVERED and give no reason: {offenders}"


class TestTheCheckerWouldNotice:
    """A test that can only pass is not a test."""

    def test_a_missing_module_does_not_resolve(self) -> None:
        assert not resolves("apps/nope/tests/test_nope.py")

    def test_a_missing_class_does_not_resolve(self) -> None:
        assert not resolves("tests/test_security_audit.py::TestNoSuchClass")

    def test_a_real_class_does_resolve(self) -> None:
        assert resolves("tests/test_security_audit.py::TestTheAuditIsComplete")

    def test_the_baseline_parser_finds_bullets(self) -> None:
        """If the parser silently found nothing, every set comparison above
        would compare two empty sets and pass."""
        assert len(baseline_ids()) > 20
        assert len(spec_19_ids()) == 7
