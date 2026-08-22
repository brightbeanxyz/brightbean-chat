"""ROADMAP contract 3, asserted structurally rather than promised in prose.

    ``conversation.automation_paused_until``, ``identity.window_expires_at`` and
    ``identity.opted_out_at`` are written **only via the messaging service
    facade / ingest pipeline**; the trigger matcher and every other consumer
    only read them.

A second write site for any of the three is invisible in code review and is a
compliance failure rather than a bug: a messaging window that reopens itself
(SPEC §8 says the ingest path updates it "and nowhere else"), an opt-out that
un-sets itself (SPEC §19 puts opt-out here so it "cannot be bypassed"), or an
automation pause a caller sets without taking over the conversation.

So the check is a source scan, over the AST rather than a grep: an assignment
target and a ``Model.objects.update(field=...)`` keyword are both writes, while
``.filter(window_expires_at__gt=...)``, an annotation and a plain attribute read
are not, and no regex tells those apart. The repo already pins structural facts
this way — see ``apps/common/tests/test_review_fixes.py``.
"""

import ast
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[3] / "apps"

#: field -> the modules allowed to write it, relative to ``apps/``.
WRITE_SITES: dict[str, set[str]] = {
    # SPEC §8: "updated on every inbound event in the webhook path. Nowhere else."
    "window_expires_at": {"messaging/ingest.py"},
    # Set by an inbound opt_out event; PR 2's facade adds no second site.
    "opted_out_at": {"messaging/ingest.py"},
    # SPEC §14's agent takeover, and L4-D's manual pause/resume toggle. Both go
    # through services.pause_automation() — the agent-send pause in particular
    # lives inside send_outbound() rather than at its caller, because contract 1
    # says so and because a caller that forgot it would leave automation
    # replying over an agent mid-conversation.
    "automation_paused_until": {"messaging/services.py"},
}


def python_sources() -> list[Path]:
    return [
        path
        for path in APPS.rglob("*.py")
        # Migrations declare the columns; tests set them up deliberately.
        if "migrations" not in path.parts and "tests" not in path.parts
    ]


def written_fields(tree: ast.AST) -> set[str]:
    """Every model field this module assigns to or updates."""
    found: set[str] = set()
    for node in ast.walk(tree):
        # self.window_expires_at = ... / identity.window_expires_at = ...
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                found.add(target.attr)
        # .update(window_expires_at=...) / .create(window_expires_at=...)
        writes = {"update", "create", "get_or_create", "update_or_create"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in writes:
            found.update(kw.arg for kw in node.keywords if kw.arg)
    return found


@pytest.mark.parametrize("field", sorted(WRITE_SITES))
def test_a_routing_field_is_written_in_one_place_only(field: str) -> None:
    allowed = WRITE_SITES[field]
    writers = set()
    for path in python_sources():
        # The module that declares the columns; a field declaration is not a
        # write. Matched on its full path, not the bare filename: skipping every
        # models.py in the project would let a save() override or a signal
        # receiver in *another* app assign one of these columns unseen, which is
        # exactly the invisible second write site this test exists to catch.
        if path.relative_to(APPS) == Path("messaging/models.py"):
            continue
        if field not in path.read_text():
            continue
        if field in written_fields(ast.parse(path.read_text())):
            writers.add(str(path.relative_to(APPS)))
    assert writers == allowed, (
        f"{field} is written in {sorted(writers)}, expected {sorted(allowed)}. "
        f"ROADMAP contract 3 makes this a single write site; if that is changing "
        f"deliberately, change WRITE_SITES in this test and say why in the PR."
    )


def test_the_scan_would_actually_catch_a_second_writer() -> None:
    """A test that can only pass is not a test. Prove the detector fires."""
    tree = ast.parse("def f(i):\n    i.window_expires_at = None\n")
    assert "window_expires_at" in written_fields(tree)
    tree = ast.parse("def f(qs):\n    qs.update(window_expires_at=None)\n")
    assert "window_expires_at" in written_fields(tree)
    # ...and does not fire on a read or a filter.
    tree = ast.parse("def f(qs, i):\n    return qs.filter(window_expires_at__gt=i.window_expires_at)\n")
    assert "window_expires_at" not in written_fields(tree)
