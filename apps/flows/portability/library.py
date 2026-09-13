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

from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["LIBRARY_RELATIVE_PATH", "gallery_entries", "library_path", "read_template", "template_paths"]

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


@lru_cache(maxsize=1)
def _gallery_entries() -> tuple[dict[str, Any], ...]:
    """The real work behind :func:`gallery_entries`, computed once per process.

    Every call reads each file in ``flow-templates/`` and runs it through
    ``parse_and_validate`` — the importer's complete schema validator. That is
    the right level of scrutiny (a shipped template earns no shortcut) and
    exactly the wrong thing to do on every render of the two most-visited pages
    in the product, which is what happened before this cache: Home and the flow
    list each called it twice, once for the slice and once for ``len()``.

    Cached for the life of the process rather than for a TTL, because the input
    is files in the repository: they change when the code does, and a worker
    that has the old set has the old code too.
    """
    return tuple(_read_gallery())


def gallery_entries() -> list[dict[str, Any]]:
    """The shipped templates, described well enough to pick one from a grid.

    A fresh list over the cached tuple, so a caller that sorts or slices cannot
    mutate what the next request sees.
    """
    return [dict(entry) for entry in _gallery_entries()]


def _read_gallery() -> list[dict[str, Any]]:
    """Walk the library and describe each template.

    Read through :func:`read_template`, so a file that stops validating
    disappears from the gallery rather than rendering a card that 500s when
    somebody clicks it. A malformed template is a broken build (a test walks
    every one of them), not a broken landing page.

    ``platforms`` is taken off the triggers because that is what a reader is
    actually choosing by: "Instagram" tells you more about whether this template
    is for you than its flow name does. Templates with no trigger — the ones
    another flow hands over to — report none, and the card simply shows no
    channel chips.
    """
    entries: list[dict[str, Any]] = []
    for path in template_paths():
        document, problems = read_template(path)
        if document is None or problems:
            continue
        entry_key = document.get("entry")
        flows = document.get("flows") or []
        flow = next((f for f in flows if f.get("key") == entry_key), flows[0] if flows else None)
        if flow is None:
            continue
        # dict.fromkeys rather than a set: the order templates list their
        # triggers in is the order a person reads the chips in, and a set would
        # reshuffle them per process.
        platforms = list(dict.fromkeys(t.get("platform") for t in flow.get("triggers") or [] if t.get("platform")))
        entries.append(
            {
                "slug": path.stem,
                "name": flow.get("name") or path.stem,
                "folder": flow.get("folder") or "",
                "platforms": platforms,
                # The platforms in words, for the tile's subtitle. Every
                # template ships in the "Starters" folder, so a tile subtitled
                # with the folder said the same thing twenty-five times; the
                # platform is what a reader is actually choosing by. A template
                # started by `api` or a rule names none, and says so, because
                # "works anywhere" is a real and useful answer.
                "platform_label": _platform_label(platforms),
                "steps": len((flow.get("graph") or {}).get("nodes") or []),
            }
        )
    # Grouped by platform so the gallery reads in runs rather than scattering
    # Instagram templates through the alphabet; by name inside each run.
    entries.sort(key=lambda entry: (entry["platform_label"] == "Any channel", entry["platform_label"], entry["name"]))
    return entries


def _platform_label(platforms: list[str]) -> str:
    """ "instagram" -> "Instagram"; two -> "Instagram and Messenger"; none -> "Any channel"."""
    from apps.common.platforms import Platform

    names = []
    for platform in platforms:
        try:
            names.append(str(Platform(platform).label))
        except ValueError:
            names.append(platform)
    if not names:
        return "Any channel"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"
