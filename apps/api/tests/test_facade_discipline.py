"""ROADMAP contract 1, enforced for ``apps/api/routers/``.

The failure mode ``docs/agent-prompts/layer-5.md`` names for this issue is one
sentence long: *"An API that writes model fields directly is the failure mode
here."* It is not hypothetical — every write endpoint has an obvious two-line
ORM shortcut beside the facade call, and the shortcut works. What it skips is
the contract-7 event, so the symptom is not a broken endpoint; it is webhooks
that quietly stop firing for whichever field someone took the shortcut on.

``apps/messaging/tests/test_write_sites.py`` pins three specific columns
deployment-wide. This is the complementary check: the *routers* delegate, full
stop. It is an AST scan rather than a grep so a call spread over three lines
still counts, and it is scoped to the router package because that is where the
temptation lives — ``delivery.py`` and ``services.py`` legitimately own their
own tables.
"""

import ast
from pathlib import Path

import pytest

ROUTERS = Path(__file__).resolve().parents[1] / "routers"

#: Method names that write. ``get_or_create`` and friends are here as well as
#: ``save``: a queryset method that creates a row is a write site whatever it is
#: called, and the facades expose their own verbs (``create_contact``,
#: ``get_or_create_tag``) which do not collide with these.
WRITING_METHODS = frozenset(
    {
        "save",
        "create",
        "get_or_create",
        "update_or_create",
        "bulk_create",
        "bulk_update",
        "update",
        "delete",
        "add",
        "remove",
        "set",
        "clear",
    }
)


def router_modules() -> list[Path]:
    return sorted(path for path in ROUTERS.glob("*.py") if path.name != "__init__.py")


def writing_calls(tree: ast.AST) -> list[str]:
    """Every ``x.save()``-shaped call, minus the HTTP verbs.

    ``@router.delete(...)`` is a route declaration, not a database delete, and
    it is the only place in a router where one of these names is legitimately an
    attribute of something. Excluding it by the receiver's name rather than by
    the method's keeps ``queryset.delete()`` caught.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in WRITING_METHODS:
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id == "router":
            continue
        found.append(node.func.attr)
    return found


def field_assignments(tree: ast.AST) -> list[str]:
    """``contact.first_name = …`` and its augmented forms."""
    found: list[str] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        found.extend(target.attr for target in targets if isinstance(target, ast.Attribute))
    return found


def test_there_is_something_to_scan():
    """A scan over an empty file list passes vacuously; this is the guard."""
    assert len(router_modules()) >= 3


@pytest.mark.parametrize("path", router_modules(), ids=lambda path: path.name)
def test_a_router_never_writes_through_the_orm(path):
    tree = ast.parse(path.read_text())

    offenders = writing_calls(tree)

    assert offenders == [], (
        f"{path.name} calls {sorted(set(offenders))}. Routes read and delegate: every write goes through "
        f"apps.contacts.services, apps.messaging.services or apps.flows.triggers.entrypoints "
        f"(ROADMAP contract 1), because that is where contract 7's events are emitted."
    )


@pytest.mark.parametrize("path", router_modules(), ids=lambda path: path.name)
def test_a_router_never_assigns_a_model_field(path):
    tree = ast.parse(path.read_text())

    offenders = field_assignments(tree)

    assert offenders == [], f"{path.name} assigns {sorted(set(offenders))} on an object rather than calling a facade."


def test_the_scan_would_actually_catch_a_shortcut():
    """A scan that cannot fail is a scan that proves nothing."""
    shortcut = ast.parse("def view(request):\n    contact.first_name = 'Ada'\n    contact.save()\n")

    assert writing_calls(shortcut) == ["save"]
    assert field_assignments(shortcut) == ["first_name"]
