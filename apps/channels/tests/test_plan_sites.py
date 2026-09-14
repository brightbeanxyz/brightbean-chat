"""Every place a channel comes into existence applies the plan limit.

A half-enforced limit is worse than none: it reads as enforced, so nobody looks
again. This app is the one at real risk of that, because there is **no**
``create_connection`` service — ``connection_create`` builds its row through a
ModelForm and each of the six adapter views constructs ``ChannelConnection(...)``
itself. Seven places, plus an eighth (re-enabling a disabled connection) that has
exactly the same effect on the count.

So this is a source scan rather than a promise, in the shape
``apps/messaging/tests/test_write_sites.py`` established for the three compliance
fields and ``tests/test_ssrf_call_sites.py`` for egress. It answers two questions
a reviewer cannot answer by reading:

1. Is the set of modules that create a connection still the set we think it is?
   A seventh platform lands as a new ``views_<platform>.py``, and the failure
   mode of forgetting the guard there is silent.
2. Does each of those modules actually call the guard?

Structural, over the AST: a docstring mentioning ``ChannelConnection`` is not a
construction, and no regex separates those.
"""

import ast
from pathlib import Path

import pytest

CHANNELS = Path(__file__).resolve().parents[1]

#: Every module allowed to bring a channel connection into existence, and what
#: each one is. Adding a file here is a decision somebody has to make.
GUARDED_MODULES: dict[str, str] = {
    "views.py": (
        "connection_create builds its row through ChannelConnectionForm rather than constructing "
        "one, so it does not appear in the construction scan below — and connection_set_status "
        "re-enables a disabled connection, which changes the count by one just as connecting does."
    ),
    "views_telegram.py": "Bot token connect flow.",
    "views_instagram.py": "OAuth callback.",
    "views_messenger.py": "Page connect flow.",
    "views_whatsapp.py": "Phone number connect flow.",
    "views_sms.py": "Twilio credentials connect flow.",
    "views_email.py": "SMTP / Resend / SES connect flow.",
}

#: The guard every one of them has to call.
GUARD = "plan_refusal"


def _sources() -> list[Path]:
    return [
        p
        for p in CHANNELS.rglob("*.py")
        if "migrations" not in p.parts and "tests" not in p.parts and p.name != "plan.py"
    ]


def _constructs_connection(tree: ast.AST) -> bool:
    """Whether this module calls ``ChannelConnection(...)`` as a constructor."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChannelConnection":
            return True
    return False


def _calls_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == GUARD:
            return True
    return False


def _parsed() -> dict[str, ast.AST]:
    return {p.name: ast.parse(p.read_text()) for p in _sources()}


class TestEveryCreationSiteIsGuarded:
    def test_the_set_of_constructing_modules_has_not_grown(self) -> None:
        """A seventh platform must land in the table above, deliberately."""
        constructing = {name for name, tree in _parsed().items() if _constructs_connection(tree)}

        assert constructing <= set(GUARDED_MODULES), (
            f"{sorted(constructing - set(GUARDED_MODULES))} constructs a ChannelConnection and is not "
            f"recorded in GUARDED_MODULES. Add the plan guard (apps/channels/plan.py) and list it here."
        )

    @pytest.mark.parametrize("module", sorted(GUARDED_MODULES))
    def test_each_one_calls_the_guard(self, module: str) -> None:
        tree = _parsed().get(module)

        assert tree is not None, f"{module} is listed in GUARDED_MODULES but does not exist"
        assert _calls_guard(tree), (
            f"{module} creates or re-enables a channel connection without calling {GUARD}(). "
            f"A limit enforced at seven of eight sites reads as enforced and is not."
        )

    def test_every_adapter_in_the_table_really_constructs_one(self) -> None:
        """The other direction. A stale entry here would let a real gap hide
        behind a name that no longer creates anything."""
        parsed = _parsed()
        adapters = {name for name in GUARDED_MODULES if name != "views.py"}
        not_constructing = {name for name in adapters if not _constructs_connection(parsed[name])}

        assert not_constructing == set(), (
            f"{sorted(not_constructing)} is listed as a connection-creating adapter but constructs none. "
            f"Either it moved to a service — in which case gate that instead — or the entry is stale."
        )


class TestTheScanWouldCatchAGap:
    """A test that can only pass is not a test. Runs the real detectors."""

    def test_it_sees_a_construction(self) -> None:
        rogue = ast.parse("def connect(w):\n    return ChannelConnection(workspace=w)\n")

        assert _constructs_connection(rogue) is True

    def test_a_docstring_mentioning_the_model_is_not_a_construction(self) -> None:
        prose = ast.parse('"""Builds a ChannelConnection(...) eventually."""\nx = 1\n')

        assert _constructs_connection(prose) is False

    def test_it_sees_a_missing_guard(self) -> None:
        rogue = ast.parse("def connect(w):\n    return ChannelConnection(workspace=w)\n")

        assert _calls_guard(rogue) is False
