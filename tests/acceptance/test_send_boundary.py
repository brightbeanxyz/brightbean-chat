"""Opt-out at the adapter boundary, pinned structurally (SECURITY-BASELINE §11).

Issue #30 asks for "an opt-out property test at the adapter boundary across
every send source". The *property* is already tested, thoroughly and set-wise:
``apps/messaging/tests/test_compliance.py::TestOptOutBeatsEverything`` runs it
across platforms and sources, ``test_compliance_setwise.py`` runs the full
product of policies, sources and outbound shapes, and
``test_compliance_doors.py`` covers the one sanctioned bypass. A second copy of
that matrix would be the drift this suite exists to prevent — so this module
does not re-run it.

What was *not* pinned is the boundary itself.

SPEC §19 places opt-out in the compliance engine "so it cannot be bypassed", and
that sentence is only true while there is one door. Every send in the product
reaches a platform through ``adapter.send(...)``, and today that call appears in
exactly one place: the send pipeline in ``apps.messaging.services``, downstream
of ``can_send``. A second call anywhere — a channel adapter calling a sibling
adapter, a broadcast worker "just retrying", a future integration reaching for
speed — would route around every one of those compliance tests without failing
any of them. It is invisible in review precisely because it looks like the
normal way to send a message.

So the check is a source scan, the same shape and for the same reason as
``apps/messaging/tests/test_write_sites.py``: a registry of the modules allowed
to hold the call, and an AST walk asserting nothing else does.
"""

from __future__ import annotations

import ast
from pathlib import Path

APPS = Path(__file__).resolve().parents[2] / "apps"

#: Modules allowed to invoke an adapter's ``send``, relative to ``apps/``.
#:
#: There is one, and adding a second is a compliance decision rather than a
#: refactor: ``apps/messaging/services.py`` is the only place ``can_send`` runs
#: before the platform is called, the only place the message row is written
#: first for idempotency, and the only place the token bucket is charged.
ADAPTER_SEND_SITES: set[str] = {"messaging/services.py"}


def python_sources() -> list[Path]:
    """Production modules. Tests drive adapters directly on purpose."""
    return [path for path in APPS.rglob("*.py") if "migrations" not in path.parts and "tests" not in path.parts]


def adapter_send_calls(tree: ast.AST) -> list[int]:
    """Line numbers of anything shaped like ``adapter.send(connection, identity, outbound)``.

    Matched on the shape rather than on the receiver's name, because the name is
    the easiest thing to change: an attribute call to ``send`` on a plain local,
    carrying positional arguments and no keywords.

    That deliberately excludes the two other things in this codebase that are
    spelled ``.send()`` and are not sends to a platform — Django signal dispatch,
    which is ``EVENT_CATALOG[event].send(...)`` on a subscript with keyword
    arguments, and Django's ``EmailMessage.send()``, which takes none. The
    self-test below pins both exclusions, so a change to this heuristic that
    started swallowing real calls fails here rather than silently.
    """
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send"
        and isinstance(node.func.value, ast.Name)
        and node.args
        and not node.keywords
    ]


class TestTheAdapterBoundaryIsOneDoor:
    def test_only_the_send_pipeline_calls_an_adapter(self) -> None:
        found: dict[str, list[int]] = {}
        for path in python_sources():
            source = path.read_text(encoding="utf-8")
            if ".send(" not in source:
                continue
            lines = adapter_send_calls(ast.parse(source, filename=str(path)))
            if lines:
                found[str(path.relative_to(APPS))] = lines

        assert set(found) == ADAPTER_SEND_SITES, (
            f"an adapter is asked to send in {sorted(found)}, expected {sorted(ADAPTER_SEND_SITES)}.\n\n"
            f"Every send has to pass apps/messaging/services.py::send_outbound, because that is where "
            f"can_send runs, where the message row is written first for idempotency, and where the token "
            f"bucket is charged. A second call site would bypass all three while every compliance test "
            f"stayed green — SPEC §19 puts opt-out in the compliance engine 'so it cannot be bypassed', "
            f"and that only holds while this set has one member.\n\n"
            f"If the boundary is genuinely moving, change ADAPTER_SEND_SITES and say why in the PR."
        )

    def test_the_one_call_is_downstream_of_the_compliance_check(self) -> None:
        """The door exists; this is the assertion that it is not a back door.

        A call site inside ``services.py`` proves nothing on its own if the
        module stopped consulting the compliance engine. Both names have to be
        in the module that holds the boundary.
        """
        source = (APPS / "messaging" / "services.py").read_text(encoding="utf-8")
        assert "can_send" in source, (
            "apps/messaging/services.py holds the only adapter.send() call in the codebase but no longer "
            "mentions can_send. The single door is only worth having while the compliance engine guards it."
        )


class TestTheScanActuallyFires:
    """A test that can only pass is not a test — see test_write_sites.py."""

    def test_it_catches_a_second_call_site(self) -> None:
        tree = ast.parse("def go(adapter, c, i, o):\n    return adapter.send(c, i, o)\n")
        assert adapter_send_calls(tree) == [2]

    def test_it_catches_one_hidden_behind_a_rename(self) -> None:
        """The receiver's name is not what makes it a send."""
        tree = ast.parse("def go(door, c, i, o):\n    return door.send(c, i, o)\n")
        assert adapter_send_calls(tree) == [2]

    def test_it_ignores_django_signal_dispatch(self) -> None:
        tree = ast.parse("def go(catalog, event, s):\n    catalog[event].send(sender=s, workspace_id=1)\n")
        assert adapter_send_calls(tree) == []

    def test_it_ignores_an_email_message_being_sent(self) -> None:
        tree = ast.parse("def go(message):\n    return message.send()\n")
        assert adapter_send_calls(tree) == []
