"""Exists, but one of its own imports does not.

Stands in for a sibling app that is installed and broken — the case
import_module reports as ImportError just like a module that is simply absent.
"""

import apps.nonexistent_dependency_for_the_probe  # noqa: F401


def job() -> str:
    return "unreachable"
