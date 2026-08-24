"""SPEC §21's acceptance criteria, as a registry rather than a claim.

Issue #30 asks for a traceability table mapping every §21 criterion to a named
test. A table in a README is a *claim*: it is written once and it is wrong the
first time somebody renames a test class. This module is the same table
expressed so CI can check it, in the shape ``tests/idor.py`` already uses — a
registry of curated rows, a derivation from the source of truth, and an
assertion that the two agree, with no silent skips.

Two things are derived rather than declared:

* **The clauses** come from ``docs/SPEC.md`` §21, parsed. A criterion cannot be
  quietly dropped from the table, and rewording §21 turns this red naming the
  clause that lost its owner — which is right, because editing an acceptance
  criterion should force somebody to look at what verifies it.
* **Collection rules** come from pytest's own ini values, so a target is checked
  against the way this repo actually collects tests rather than against three
  string literals that could drift from ``pyproject.toml``.

Targets resolve by parsing the AST, never by importing. Importing a test module
runs its body, and here that is not hypothetical: ``apps/common/tests/
test_settings_boot.py`` writes a module into the source tree at import time, and
``pyproject.toml`` carries a comment about the consequences. The AST gives
identical rename- and delete-detection for none of that risk;
``apps/messaging/tests/test_write_sites.py`` is the precedent for pinning a
structural fact with a source scan.

**Nothing here asserts anything about unmerged work.** A ``PENDING`` row records
that a criterion's owner has not landed and names the issue that owns it. It
does not assert the owner's code is absent — that would turn this suite red on a
sibling's *correct* merge, which is exactly the "goes red at random, so it gets
ignored" failure issue #30 warns about.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "BLOCKING_ISSUES",
    "CRITERIA",
    "SECURITY_GATES",
    "SECURITY_PHASE",
    "CollectionRules",
    "Criterion",
    "UnjustifiedPendingError",
    "UnresolvableVerificationError",
    "Verification",
    "all_rows",
    "readme_ids",
    "resolve",
    "spec_clauses",
    "spec_inline_http_timeout",
    "spec_latency_budgets",
    "unclaimed_clauses",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "docs" / "SPEC.md"
README_PATH = Path(__file__).resolve().parent / "README.md"

#: Phase number used by rows that are not SPEC §21 clauses at all — the
#: cross-system security gates issue #30 asks for alongside them.
SECURITY_PHASE = 0


class UnresolvableVerificationError(AssertionError):
    """A row names a test or a runbook heading that does not exist."""


class UnjustifiedPendingError(AssertionError):
    """A PENDING row without a blocking issue explained in BLOCKING_ISSUES."""


class Verification(StrEnum):
    """How a criterion is verified. There is no fourth option, and no 'unknown'."""

    #: A test the per-PR suite runs. ``targets`` are pytest node ids.
    CI = "ci"
    #: A procedure a maintainer performs. ``targets`` are runbook headings.
    MANUAL = "manual"
    #: Nothing verifies it yet, and ``blocked_by`` says who will.
    PENDING = "pending"


#: Issue -> why this suite is not writing that criterion's test itself. A
#: PENDING row whose ``blocked_by`` is missing from here refuses to be
#: constructed: the same rule ``tests/idor.py``'s ``WAIVED_ROUTES`` applies to
#: an un-swept route, for the same reason. An exemption is a reviewed line of
#: code carrying a prose reason, or it is not an exemption.
BLOCKING_ISSUES: dict[int, str] = {
    27: (
        "L7-B owns flow export/import, and its own acceptance criteria already "
        "include 'Round-trip test incl. triggers per §21 phase 3, for each "
        "starter template' plus 'Exports contain zero workspace-identifying "
        "data'. Writing a second round-trip here would be the duplication this "
        "suite exists to avoid, and pinning the *absence* of #27's seam would "
        "turn main red the day #27 merges correctly. So this row stays PENDING "
        "and a human flips it to CI when #27 lands — a missed flip reads as "
        "'not yet verified', which is the safe direction to be wrong in. Note "
        "for whoever flips it: apps/flows/schema/export.py and "
        "apps/flows/tests/test_export.py are the JSON-Schema *artifact*, "
        "unrelated to flow documents, and apps/flows/services.py's "
        "duplicate_flow deliberately does not copy triggers."
    ),
}


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion and the thing that verifies it."""

    #: Stable handle, used in the README and in failure messages.
    id: str
    #: 1, 2 or 3 for a SPEC §21 phase; ``SECURITY_PHASE`` for a security gate.
    phase: int
    #: For a §21 row, the clause **verbatim** from the spec — it is matched
    #: against the parsed text, so a paraphrase fails. For a security gate,
    #: free prose naming the gate.
    clause: str
    verification: Verification
    #: Why these targets stand for this clause. The part a reader needs and the
    #: part no assertion can supply.
    note: str
    targets: tuple[str, ...] = ()
    blocked_by: int | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(p[123]|sec)-[a-z0-9-]+", self.id):
            raise ValueError(f"criterion id {self.id!r} should look like 'p1-first-reply' or 'sec-idor-sweep'")
        if self.verification is Verification.PENDING:
            if self.targets:
                # UnjustifiedPendingError, not ValueError: every way of getting a
                # PENDING row wrong raises the same type, so one ``except`` clause
                # covers the class rather than two of the three cases.
                raise UnjustifiedPendingError(f"criterion {self.id!r} is PENDING and cannot name a target")
            if self.blocked_by is None:
                raise UnjustifiedPendingError(
                    f"criterion {self.id!r} is PENDING with no blocked_by. Name the issue that owns it."
                )
            if self.blocked_by not in BLOCKING_ISSUES:
                raise UnjustifiedPendingError(
                    f"criterion {self.id!r} is blocked on #{self.blocked_by}, which is not in BLOCKING_ISSUES. "
                    f"Add a line naming the issue and why this suite is not writing the test itself — a pending "
                    f"criterion with no stated owner is a silent skip."
                )
        elif not self.targets:
            raise ValueError(f"criterion {self.id!r} is {self.verification} and must name at least one target")
        else:
            self._check_target_kinds()

    def _check_target_kinds(self) -> None:
        """A CI row must name a test and a MANUAL row must name a runbook.

        ``resolve()`` dispatches on whether a target contains ``#``, not on what
        the row claims to be, so without this a CI row could point at a heading
        in the README and resolve perfectly — and the table would report the
        criterion as verified in CI when nothing runs. That is the same
        overstatement the p95 and 10k rows were split to stop, one level up.
        """
        for target in self.targets:
            looks_like_a_runbook = "#" in target
            if self.verification is Verification.CI and looks_like_a_runbook:
                raise ValueError(
                    f"criterion {self.id!r} is CI but names {target!r}, which is a runbook anchor. "
                    f"A CI row has to name a pytest node id — otherwise the gate reports a criterion as "
                    f"tested when the only thing behind it is prose."
                )
            if self.verification is Verification.CI and not target.split("::")[0].endswith(".py"):
                raise ValueError(
                    f"criterion {self.id!r} is CI but names {target!r}, which is not a Python module. "
                    f"A CI target is 'path.py', 'path.py::TestClass' or 'path.py::TestClass::test_x'."
                )
            if self.verification is Verification.MANUAL and not looks_like_a_runbook:
                raise ValueError(
                    f"criterion {self.id!r} is MANUAL but names {target!r}, which is not a runbook anchor. "
                    f"A MANUAL row has to name 'docs/file.md#a-heading' so a maintainer can find the "
                    f"procedure that stands in for a test."
                )


@dataclass(frozen=True)
class CollectionRules:
    """The globs pytest itself collects by, read from the ini rather than guessed.

    Every field is required. Defaults would be a second, silent copy of
    ``pyproject.toml``'s ``[tool.pytest.ini_options]`` that nothing keeps in
    step — build these with :meth:`from_config` and let pytest be the authority.
    """

    files: tuple[str, ...]
    classes: tuple[str, ...]
    functions: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Any) -> CollectionRules:
        return cls(
            files=tuple(config.getini("python_files")),
            classes=tuple(config.getini("python_classes")),
            functions=tuple(config.getini("python_functions")),
        )

    @staticmethod
    def _matches(name: str, patterns: tuple[str, ...]) -> bool:
        """pytest's own rule: a pattern with glob characters is a glob, otherwise a prefix."""
        for pattern in patterns:
            if any(char in pattern for char in "*?["):
                if fnmatch.fnmatch(name, pattern):
                    return True
            elif name.startswith(pattern):
                return True
        return False

    def collects_file(self, name: str) -> bool:
        return self._matches(name, self.files)

    def collects_class(self, name: str) -> bool:
        return self._matches(name, self.classes)

    def collects_function(self, name: str) -> bool:
        return self._matches(name, self.functions)


# --------------------------------------------------------------------------
# Deriving the clauses from SPEC §21
# --------------------------------------------------------------------------

_SECTION = re.compile(r"^## 21\..*?(?=^## 22\.)", re.MULTILINE | re.DOTALL)
_PHASE = re.compile(r"^### Phase (\d)\s*$", re.MULTILINE)


def spec_clauses(spec_path: Path | None = None) -> dict[int, tuple[str, ...]]:
    """Every ``Accept when:`` clause in SPEC §21, per phase.

    The spec writes them as one semicolon-separated sentence per phase, which is
    prose and therefore the fragile half of this module. It is worth it: without
    it the registry is a list somebody curated once, and a criterion that never
    got a row would be invisible. The failure message prints what was parsed, so
    a punctuation change reads as "here is what I found" rather than as a puzzle.
    """
    text = (spec_path or SPEC_PATH).read_text(encoding="utf-8")
    section = _SECTION.search(text)
    if section is None:
        raise AssertionError(f"could not find §21 between '## 21.' and '## 22.' in {spec_path or SPEC_PATH}")

    body = section.group(0)
    found: dict[int, tuple[str, ...]] = {}
    splits = list(_PHASE.finditer(body))
    for index, match in enumerate(splits):
        phase = int(match.group(1))
        end = splits[index + 1].start() if index + 1 < len(splits) else len(body)
        chunk = body[match.end() : end]
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped.startswith("Accept when:"):
                continue
            sentence = stripped.removeprefix("Accept when:").strip().rstrip(".")
            clauses = tuple(part.strip() for part in sentence.split(";") if part.strip())
            found[phase] = found.get(phase, ()) + clauses
    return found


#: The two latency numbers §21 phase 1 states, in seconds, read out of the spec
#: rather than copied into a test. Both budgets are asserted against these, so
#: changing the spec's number changes what the tests demand — which is the point
#: of an acceptance suite, and the alternative is a literal that silently stops
#: agreeing with the document it came from.
_BUDGET_PATTERNS = {
    "ack": re.compile(r"webhook ack p95 < (\d+(?:\.\d+)?) ms"),
    "first_reply": re.compile(r"first automated reply p95 < (\d+(?:\.\d+)?) s"),
}


def spec_latency_budgets() -> dict[str, float]:
    """``{"ack": 0.5, "first_reply": 2.0}``, parsed from SPEC §21 phase 1."""
    text = " ".join(spec_clauses().get(1, ()))
    budgets: dict[str, float] = {}
    for name, pattern in _BUDGET_PATTERNS.items():
        match = pattern.search(text)
        if match is None:
            raise AssertionError(f"could not read the {name} budget out of SPEC §21 phase 1. The clause said: {text!r}")
        budgets[name] = float(match.group(1)) / (1000 if name == "ack" else 1)
    return budgets


#: SPEC §7.1's "2 s hard timeout on the HTTP client" — a different budget from
#: §21's reply ceiling that happens to share its value today. Parsed rather than
#: copied for the same reason the ceiling is: a literal stops agreeing with the
#: document it came from and nothing says so.
_HARD_HTTP_TIMEOUT = re.compile(r"\((\d+(?:\.\d+)?) s hard timeout on the HTTP client\)")


def spec_inline_http_timeout(spec_path: Path | None = None) -> float:
    """The hard timeout SPEC §7.1 puts on an inline outbound call, in seconds."""
    text = (spec_path or SPEC_PATH).read_text(encoding="utf-8")
    match = _HARD_HTTP_TIMEOUT.search(text)
    if match is None:
        raise AssertionError(
            f"could not find §7.1's '(N s hard timeout on the HTTP client)' in {spec_path or SPEC_PATH}"
        )
    return float(match.group(1))


def unclaimed_clauses() -> dict[int, tuple[str, ...]]:
    """Clauses in §21 that no row owns. Empty is the only acceptable answer."""
    claimed = {(row.phase, row.clause) for row in CRITERIA}
    unclaimed: dict[int, tuple[str, ...]] = {}
    for phase, clauses in spec_clauses().items():
        missing = tuple(clause for clause in clauses if (phase, clause) not in claimed)
        if missing:
            unclaimed[phase] = missing
    return unclaimed


# --------------------------------------------------------------------------
# Resolving a target
# --------------------------------------------------------------------------


def _slug(heading: str) -> str:
    """GitHub's anchor rule, near enough: lowercase, drop punctuation, spaces to hyphens."""
    cleaned = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"[\s_]+", "-", cleaned)


def _headings(markdown: str) -> set[str]:
    """Anchors for the ATX headings in ``markdown``, ignoring fenced code.

    The fence tracking is not decoration. A ``#`` at the start of a line inside a
    ```` ```bash ```` block is a shell comment, not a heading, and counting it
    would mint an anchor that resolves here and 404s in a browser — turning a
    gate built to catch stale pointers into one that manufactures them.
    """
    headings: set[str] = set()
    fenced = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fenced = not fenced
            continue
        if not fenced and line.startswith("#"):
            headings.add(_slug(line.lstrip("#")))
    return headings


def _resolve_runbook(target: str) -> None:
    relative, _, anchor = target.partition("#")
    path = REPO_ROOT / relative
    if not path.is_file():
        raise UnresolvableVerificationError(f"{target}: no such file as {relative}")
    if not anchor:
        return
    headings = _headings(path.read_text(encoding="utf-8"))
    if anchor not in headings:
        raise UnresolvableVerificationError(
            f"{target}: {relative} has no heading anchored '{anchor}'. It has: {sorted(headings)}. "
            f"A renamed heading is a runbook a maintainer cannot find."
        )


def _definitions(body: list[ast.stmt]) -> dict[str, ast.stmt]:
    return {node.name: node for node in body if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)}


def resolve(target: str, rules: CollectionRules) -> None:
    """Raise ``UnresolvableVerificationError`` unless pytest would collect ``target``.

    Accepts ``path.py``, ``path.py::TestClass`` and ``path.py::TestClass::test_x``
    (or ``path.py::test_x`` for a bare function). Deliberately refuses a
    parametrized id: those churn every time a parametrize list is edited, and a
    traceability row should survive that.

    ``rules`` is required rather than defaulting. A default would be three
    string literals standing in for ``pyproject.toml``'s collection settings,
    which is exactly the drift reading them from pytest was meant to remove:
    a caller who forgot the argument would silently evaluate against a frozen
    copy and reject a legitimate target with a message quoting rules that are
    not in force.
    """
    if "#" in target:
        _resolve_runbook(target)
        return

    segments = target.split("::")
    if len(segments) > 3:
        raise UnresolvableVerificationError(
            f"{target}: a node id goes at most three deep — 'path.py', 'path.py::TestClass' or "
            f"'path.py::TestClass::test_x'. Anything past that was silently ignored before this check, "
            f"so a typo'd target resolved clean while naming something pytest would never collect."
        )
    if any("[" in segment for segment in segments):
        raise UnresolvableVerificationError(
            f"{target}: parametrized ids churn whenever the parametrize list is edited. "
            f"Name the class, or the function without its parameters."
        )

    relative, rest = segments[0], segments[1:]
    path = REPO_ROOT / relative
    if not rules.collects_file(path.name):
        raise UnresolvableVerificationError(
            f"{target}: pytest does not collect {path.name} (python_files = {list(rules.files)})"
        )
    if not path.is_file():
        raise UnresolvableVerificationError(f"{target}: no such file as {relative}")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if not rest:
        return

    head, *tail = rest
    top = _definitions(tree.body)
    if rules.collects_class(head):
        node = top.get(head)
        if not isinstance(node, ast.ClassDef):
            raise UnresolvableVerificationError(
                f"{target}: {relative} defines no class {head}. It defines: "
                f"{sorted(name for name, item in top.items() if isinstance(item, ast.ClassDef))}. "
                f"A test that moved must move its row in tests/acceptance/criteria.py too — a stale row "
                f"means this gate reads green on a test that no longer exists."
            )
        if not tail:
            return
        method = _definitions(node.body).get(tail[0])
        if method is None or not rules.collects_function(tail[0]):
            raise UnresolvableVerificationError(
                f"{target}: {head} has no test method {tail[0]}. It has: "
                f"{sorted(name for name in _definitions(node.body) if rules.collects_function(name))}"
            )
        return

    if not rules.collects_function(head):
        raise UnresolvableVerificationError(
            f"{target}: {head} is neither a collected class ({list(rules.classes)}) "
            f"nor a collected function ({list(rules.functions)})"
        )
    if head not in top:
        raise UnresolvableVerificationError(f"{target}: {relative} defines no function {head}")


def readme_ids(readme_path: Path | None = None) -> set[str]:
    """Every criterion id the README mentions in backticks."""
    text = (readme_path or README_PATH).read_text(encoding="utf-8")
    return set(re.findall(r"`((?:p[123]|sec)-[a-z0-9-]+)`", text))


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

_PHASE_1_LATENCY = "webhook ack p95 < 500 ms and first automated reply p95 < 2 s on a 2 vCPU box"
_PHASE_3_SCENARIO = (
    "a Make/Zapier-style scenario (inbound webhook -> API contact update -> flow start) "
    "works with only public API + outbound webhooks"
)

#: SPEC §21, phases 1-3. Every clause the spec states is owned by at least one
#: row here, and ``test_criteria.py`` fails if that stops being true.
CRITERIA: tuple[Criterion, ...] = (
    # ---------------------------------------------------------------- phase 1
    Criterion(
        id="p1-ack-budget",
        phase=1,
        clause=_PHASE_1_LATENCY,
        verification=Verification.CI,
        targets=("apps/flows/tests/test_routing_inline.py::TestAckLatency",),
        note=(
            "The ack half. Routing cannot predict a slow platform, so it declines to start one: the "
            "courtesy calls probe the same API the send will use, and one overrun flags the connection "
            "so later events enqueue before any I/O. Measured against a genuinely slow adapter."
        ),
    ),
    Criterion(
        id="p1-first-reply",
        phase=1,
        clause=_PHASE_1_LATENCY,
        verification=Verification.CI,
        targets=("tests/acceptance/test_first_reply_latency.py::TestFirstReplyLatency",),
        note=(
            "The reply half, which nothing measured before this suite. Times a signed webhook POST "
            "through to the reply on the wire. See this directory's README for why the assertion is on "
            "the minimum and the p95 comes from a reference run."
        ),
    ),
    Criterion(
        id="p1-first-reply-budget",
        phase=1,
        clause=_PHASE_1_LATENCY,
        verification=Verification.CI,
        targets=("tests/acceptance/test_first_reply.py::TestTheReplyStaysInsideTheSpecBudget",),
        note=(
            "The arithmetic behind the number, and the half that still means something on a machine "
            "nobody benchmarked: the inline budget and the adapter's HTTP timeouts both have to fit "
            "inside the spec's ceiling, or the criterion is unmeetable however fast the code is. "
            "Nothing else tied those constants to §21. The behaviour they produce — a slow connection "
            "flagged and then enqueued — is already covered by test_routing_inline.py::TestTheGates."
        ),
    ),
    Criterion(
        id="p1-first-reply-p95",
        phase=1,
        clause=_PHASE_1_LATENCY,
        verification=Verification.MANUAL,
        targets=("tests/acceptance/README.md#the-2-vcpu-reference-run",),
        note=(
            "The p95 figure itself, which CI does not produce and should not pretend to. The clause says "
            "'on a 2 vCPU box'; CI runs four xdist workers on a shared runner, and a p95 over the handful "
            "of samples a unit test can afford is the maximum by another name. So CI asserts the floor and "
            "the median against the ceiling — which catches a regression — and the distribution the spec "
            "actually names comes from a reference run whose numbers live in the README."
        ),
    ),
    Criterion(
        id="p1-no-duplicate-sends",
        phase=1,
        clause="zero duplicate sends across 1k forced worker retries",
        verification=Verification.CI,
        targets=(
            "apps/messaging/tests/test_services.py::TestIdempotency",
            "apps/broadcasts/tests/test_fanout.py",
        ),
        note=(
            "Counted on the fake adapter rather than inferred from message rows: a thousand forced "
            "retries make exactly one provider call. The broadcast module carries the same criterion "
            "for fanout, where the queue's idempotency key is the second belt."
        ),
    ),
    Criterion(
        id="p1-no-interleaving",
        phase=1,
        clause="concurrent webhooks for one contact never interleave steps (test with 50 parallel events)",
        verification=Verification.CI,
        targets=("apps/flows/tests/test_locking.py::TestOneStepPerContact",),
        note=(
            "50 real threads against the real advisory lock. Interleaving is detected structurally — the "
            "node records enter/leave and the test counts nesting depth — so the sleep inside it widens "
            "the window and is never itself the thing asserted on."
        ),
    ),
    Criterion(
        id="p1-ig-private-reply",
        phase=1,
        clause="IG private-reply constraints enforced in tests",
        verification=Verification.CI,
        targets=(
            "apps/flows/tests/test_trigger_guards.py::TestCommentGuard",
            "apps/flows/tests/test_trigger_guards.py::TestCommentGuardUnderConcurrency",
            "apps/channels/tests/test_instagram_comments.py::TestTheClaimIsNotAGeneralLicence",
            "apps/messaging/tests/test_compliance.py::TestOutsideTheWindow",
        ),
        note=(
            "Three constraints, four homes. One private reply per comment is the claim guard, and the "
            "issue asks for it under concurrency specifically, so both guard classes are named. A spent "
            "claim cannot be readdressed by a later send on the thread it opened. And the seven-day "
            "human-agent allowance is the only thing that reopens a closed window, which is what stops "
            "automation borrowing an agent's licence."
        ),
    ),
    Criterion(
        id="p1-loop-cap",
        phase=1,
        clause="loop flow halts at 30 blocks with admin notification",
        verification=Verification.CI,
        targets=(
            "apps/flows/tests/test_loop_cap.py::TestLoopCap",
            "apps/notifications/tests/test_queue.py",
        ),
        note=(
            "Exact equality on the block count, not an upper bound — 'halts at 30' is a specific number "
            "and an off-by-one is a real defect. The notification half asserts the admin is a recipient "
            "and a viewer is not."
        ),
    ),
    # ---------------------------------------------------------------- phase 2
    Criterion(
        id="p2-broadcast-scale",
        phase=2,
        clause=("10k-contact broadcast respects token buckets and skips out-of-window identities with correct counts"),
        verification=Verification.CI,
        targets=("apps/broadcasts/tests/test_acceptance.py::TestTenThousandContactBroadcast",),
        note=(
            "The *mechanics* of the clause — token buckets respected, out-of-window identities skipped, "
            "counts reconciling, cancellation clean — at 600 contacts: one full 500-row chunk plus a "
            "partial, which is what exercises the chunking arithmetic. It does not verify the scale. A "
            "regression that capped fanout near a thousand, or stopped scheduling successors after the "
            "second chunk, would leave this green, which is why the row below exists."
        ),
    ),
    Criterion(
        id="p2-broadcast-ten-thousand",
        phase=2,
        clause=("10k-contact broadcast respects token buckets and skips out-of-window identities with correct counts"),
        verification=Verification.MANUAL,
        targets=("tests/acceptance/README.md#the-10k-contact-broadcast-run",),
        note=(
            "The scale the clause names. A third chunk adds about ninety seconds to every CI run and 10k "
            "would add far more, for assertions the 600-contact run already makes — so the size itself is "
            "verified by a documented run rather than on every pull request. Splitting it from the row "
            "above is the point: one table saying '10k, verified in CI' when the test builds 600 is the "
            "kind of claim this suite exists to stop."
        ),
    ),
    Criterion(
        id="p2-whatsapp-template-waba",
        phase=2,
        clause="WhatsApp template submit->approved->send round-trips against a real WABA",
        verification=Verification.MANUAL,
        targets=("tests/acceptance/README.md#whatsapp-template-round-trip-against-a-real-waba",),
        note=(
            "The lifecycle is covered against a fake Graph API in "
            "apps/channels/tests/test_whatsapp_templates.py, but the criterion says *a real WABA*: it "
            "needs Meta credentials and a real review turnaround, so no amount of test infrastructure "
            "makes it a CI job. The runbook is the verification."
        ),
    ),
    Criterion(
        id="p2-sms-stop",
        phase=2,
        clause="STOP suppresses within one inbound event",
        verification=Verification.CI,
        targets=("apps/channels/tests/test_sms_compliance.py::TestStop",),
        note=(
            "A signed Twilio form POST through the real endpoint, with only the HTTP transport faked. "
            "Proves all three halves in one event: the identity is opted out, the confirmation reaches "
            "the wire, and a later send — automation or broadcast — comes back failed with 'opted_out'."
        ),
    ),
    Criterion(
        id="p2-email-unsubscribe",
        phase=2,
        clause="unsubscribe link suppresses email within one click",
        verification=Verification.CI,
        targets=("tests/acceptance/test_unsubscribe_round_trip.py::TestOneClickUnsubscribe",),
        note=(
            "Both halves were already tested and never joined: one suite proved a click writes the "
            "suppression, another proved a suppressed address never reaches the transport, and nothing "
            "proved the link in a real message is the one that does it. This harvests the URL out of a "
            "composed envelope, clicks it, and then proves the next send is refused."
        ),
    ),
    # ---------------------------------------------------------------- phase 3
    Criterion(
        id="p3-zapier-scenario",
        phase=3,
        clause=_PHASE_3_SCENARIO,
        verification=Verification.CI,
        targets=("apps/api/tests/test_acceptance_phase3.py::TestPhaseThreeScenario",),
        note=(
            "Every step is an HTTP call an integrator could make with curl, and the final assertion is "
            "on the bytes a third-party receiver would verify — including a negative test proving the "
            "sample verifier rejects a body altered in flight, without which the positive one is vacuous."
        ),
    ),
    Criterion(
        id="p3-platform-inbound-chain",
        phase=3,
        clause=_PHASE_3_SCENARIO,
        verification=Verification.CI,
        targets=("tests/acceptance/test_integration_chain.py::TestTheChainFromPlatformToIntegrator",),
        note=(
            "The one leg the row above does not span. That scenario starts at POST /api/v1/contacts, so "
            "the contact is created by the API; the spec's scenario starts at an inbound webhook. This "
            "runs the whole chain — signed platform delivery, message.received on the integrator's "
            "receiver, the API legs under a bearer key, the reply back on the platform wire."
        ),
    ),
    Criterion(
        id="p3-flow-roundtrip",
        phase=3,
        clause="flow export/import round-trips including triggers",
        verification=Verification.PENDING,
        blocked_by=27,
        note=(
            "Owned by #27, which ships the feature and its own round-trip test. Flip this row to CI "
            "pointing at that test when #27 merges; BLOCKING_ISSUES[27] has the detail."
        ),
    ),
)

#: The cross-system security gates issue #30 carries alongside §21
#: (SECURITY-BASELINE §11). Not spec clauses, so they are not part of the
#: completeness check — but they resolve like everything else, so a renamed
#: security test cannot quietly leave the gate unguarded.
SECURITY_GATES: tuple[Criterion, ...] = (
    Criterion(
        id="sec-idor-sweep",
        phase=SECURITY_PHASE,
        clause="IDOR fuzz sweep across all registered endpoints as a foreign-workspace user",
        verification=Verification.CI,
        targets=("tests/test_idor.py::TestCrossTenantIsolation",),
        note=(
            "Runs per PR, not nightly — it walks the live URL conf, so it discovers a new route whether "
            "or not its author remembered this gate. The sweep has no silent skips: an unregistered "
            "kwarg raises, an unnamed tenant route raises, and a waiver is a reviewed line of code."
        ),
    ),
    Criterion(
        id="sec-optout-adapter-boundary",
        phase=SECURITY_PHASE,
        clause="Opt-out enforced at the adapter boundary across every send source",
        verification=Verification.CI,
        targets=("tests/acceptance/test_send_boundary.py::TestTheAdapterBoundaryIsOneDoor",),
        note=(
            "The compliance matrix itself is already tested set-wise over platform x source, so a "
            "second copy would be the drift this suite exists to avoid. What was not pinned is the "
            "structural half: that there is exactly one place in the codebase where an adapter is asked "
            "to send, so 'opt-out cannot be bypassed' is a property of the shape rather than of "
            "everyone's diligence."
        ),
    ),
    Criterion(
        id="sec-hostile-webhook",
        phase=SECURITY_PHASE,
        clause="Hostile webhook storm: repeated, oversized and malformed deliveries, no 5xx, single execution",
        verification=Verification.CI,
        targets=(
            "apps/channels/tests/test_webhooks.py::TestHostilePayloads",
            "apps/channels/tests/test_webhooks.py::TestDeduplication",
            "tests/acceptance/test_integration_chain.py::TestAHostileRedelivery",
        ),
        note=(
            "The channels suite covers injection, malformed shapes, oversized bodies, NUL bytes and a "
            "parser that raises — all answering 200, because a 5xx makes the platform retry the same "
            "body until it gives up on the webhook. The acceptance test adds the crossing nothing owned: "
            "a batch delivered three times, with a malformed sibling event, still starts one flow and "
            "sends one message."
        ),
    ),
    Criterion(
        id="sec-exactly-once-queue",
        phase=SECURITY_PHASE,
        clause="Worker concurrency: exactly-once processing under contended drains",
        verification=Verification.CI,
        targets=("apps/queueing/tests/test_concurrency.py::TestExactlyOnce",),
        note=(
            "1000 actions, three real drain threads, side effects recorded against a unique constraint "
            "so a duplicate raises at the moment it happens rather than being counted afterwards. It "
            "also asserts more than one worker did work, which is what stops the test passing because "
            "the concurrency never actually occurred."
        ),
    ),
    Criterion(
        id="sec-crash-recovery",
        phase=SECURITY_PHASE,
        clause="Worker killed mid-broadcast resumes without loss; postgres restart recovers via zombie sweep",
        verification=Verification.MANUAL,
        targets=("tests/acceptance/README.md#crash-and-restart-recovery",),
        note=(
            "Needs real processes to be real. An in-process 'kill' is a function that returns early, "
            "which proves the handler is re-entrant — already covered by the forced-retry tests — and "
            "not that a half-written batch survives losing its worker. Runs against a deployment."
        ),
    ),
)


def all_rows() -> tuple[Criterion, ...]:
    return CRITERIA + SECURITY_GATES
