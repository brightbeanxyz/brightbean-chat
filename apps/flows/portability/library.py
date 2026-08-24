"""The starter templates shipped in the repository, and how they are found.

``flow-templates/`` at the repository root holds flow templates anybody can
import — the seed of the shared library the issue describes, and the directory a
community pull request adds to. A test validates every file in it against the
importer and imports each into a clean workspace, so a template that stops
working is a red build rather than a download that fails for a stranger.

**Not** ``templates/``: that path is Django's own ``TEMPLATES["DIRS"]``
(``config/settings/base.py``), and putting JSON documents inside the HTML
template loader's search path would be a trap for the next person who wonders
why their template name resolves to a flow.
"""

from pathlib import Path
from typing import Any

__all__ = ["LIBRARY_RELATIVE_PATH", "library_path", "read_template", "template_paths"]

#: Where the shipped templates live, relative to the repository root.
LIBRARY_RELATIVE_PATH = Path("flow-templates")


def library_path() -> Path:
    """Absolute path of the shipped template directory."""
    from django.conf import settings

    return Path(settings.BASE_DIR) / LIBRARY_RELATIVE_PATH


def template_paths() -> list[Path]:
    """Every shipped template, in a stable order.

    Sorted rather than in directory order: a test that iterates these reports
    its failures in the same sequence on every machine, and a filesystem's idea
    of order is not one.
    """
    directory = library_path()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def read_template(path: Path) -> tuple[dict[str, Any] | None, list[Any]]:
    """One shipped template, through the same front door an upload uses.

    Deliberately :func:`apps.flows.portability.imports.parse_and_validate` and
    not a shortcut: a file this repository ships gets exactly the scrutiny a
    stranger's does, which is what makes "CI validates the templates against the
    importer" a true statement rather than a weaker one about a different code
    path.
    """
    from apps.flows.portability.imports import parse_and_validate

    return parse_and_validate(path.read_bytes())
