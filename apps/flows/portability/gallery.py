"""The Browse-templates cards: what is shippable, and what each one says.

:mod:`apps.flows.portability.library` answers "what files are there, and how do
I read one". This answers "what does the browse page show", which is a different
question with a different failure mode — the cards render inside the *flows list
empty state*, so anything that raises here 500s the core Flows page of every
brand-new workspace.

So **nothing in this module raises**. A malformed manifest, a template that
stops validating, a filename that cannot be a URL segment: each is skipped with
a log, and the gallery renders what is left. CI already makes a broken template
a red build (``manage.py validate_flow_templates`` and
``tests/test_portability_library.py``), so a failure at runtime means a
hand-edited deployment or a corrupt image — degrading is right, 500 is not.

The requirements on a card come from :func:`imports.requirements_for`, which
walks the document, and not from its ``requirements`` block: docs/flow-templates
calls that block advisory, and the walk is the authority. It needs no workspace
and no query, which is why "Needs Instagram" can be answered before anyone
clicks anything.
"""

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.common.platforms import Platform
from apps.flows.portability import imports, library

__all__ = [
    "DEFAULT_CATEGORY",
    "GALLERY_MANIFEST_NAME",
    "TemplateCard",
    "by_category",
    "connected_platforms",
    "gallery",
    "template_for_slug",
]

logger = logging.getLogger(__name__)

GALLERY_MANIFEST_NAME = "gallery.toml"

#: Where a template with no manifest entry is filed.
DEFAULT_CATEGORY = "Starters"

#: Django's ``<slug:>`` converter, exactly. A stem that does not match cannot be
#: reversed into a URL, and ``{% url %}`` raises *during template rendering*,
#: where no try/except in a view can catch it.
_SLUG = re.compile(r"^[-a-zA-Z0-9_]+$")


@dataclass(frozen=True)
class TemplateCard:
    """One card. Everything on it is derived from the document, except the prose."""

    slug: str
    filename: str
    title: str
    summary: str
    category: str
    flow_count: int
    node_count: int
    trigger_count: int
    folder: str
    #: Platforms the document needs, in walk order, as ``{key, label}``. Both,
    #: because the card tests the key against this workspace's connections and
    #: shows the label to a person. Whether a platform is *satisfied* is not on
    #: the card: cards are cached per process and connections are per workspace.
    platforms: tuple[dict[str, str], ...]
    #: Kinds the import would create, already worded ("1 tag").
    creates: tuple[str, ...]
    #: Kinds that must already exist and cannot be created for you.
    must_already_have: tuple[str, ...]
    #: Whether any node calls out to the internet.
    calls_out: bool


#: ``(fingerprint, cards)``. The fingerprint is one stat per file, so in an
#: image — where these are read-only — the library is parsed once per process
#: and every later call is three stats and a tuple compare. Under ``make
#: server`` an edited template invalidates on the next request, which is the
#: expectation Django's own autoreload sets. A benign race costs one duplicate
#: parse. Not django.core.cache: CACHE_URL defaults to the database, and a round
#: trip to avoid a three-file parse is the worse trade.
_CACHE: tuple[tuple[Any, ...], list[TemplateCard]] | None = None


def gallery() -> list[TemplateCard]:
    """Every shippable template, manifest order first, then the rest by name."""
    global _CACHE

    paths = library.template_paths()
    manifest_path = library.library_path() / GALLERY_MANIFEST_NAME
    fingerprint = _fingerprint(paths, manifest_path)
    if _CACHE is not None and _CACHE[0] == fingerprint:
        return _CACHE[1]

    entries = _manifest(manifest_path)
    cards = [card for path in paths if (card := _card(path, entries.get(path.name, {}))) is not None]
    cards.sort(key=lambda card: (_order(entries, card.filename), card.title.lower()))
    _CACHE = (fingerprint, cards)
    return cards


def by_category(cards: list[TemplateCard]) -> list[dict[str, Any]]:
    """The cards grouped for rendering, in first-seen category order."""
    groups: dict[str, list[TemplateCard]] = {}
    for card in cards:
        groups.setdefault(card.category, []).append(card)
    return [{"label": label, "cards": items} for label, items in groups.items()]


def template_for_slug(slug: str) -> Path | None:
    """The shipped file that slug names, or ``None``.

    Built from the directory listing and looked up, never joined onto a path.
    The ``<slug:>`` converter already makes ``..`` and ``/`` unable to match the
    route; this is the belt behind that brace, and it is the reason
    ``FlowImport.original_filename``'s "never used to build a path" stays true.
    """
    return {path.stem: path for path in library.template_paths()}.get(slug)


def connected_platforms(workspace: Any) -> set[str]:
    """Which platforms this workspace has a connection for.

    Mirrors ``apps.flows.picklists._connections``, which is where the same
    question is answered for the import review page — and, like it, ignores
    connection *status*: a disabled connection is still a connection you can map
    a trigger onto. Kept to one query rather than calling ``picklists()``, which
    runs six resolvers for the five answers this does not need.
    """
    from apps.flows.compat import installed_model

    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:
        return set()
    return set(model.objects.for_workspace(workspace).values_list("platform", flat=True).distinct())


# ── internals ───────────────────────────────────────────────────────────────


def _fingerprint(paths: list[Path], manifest: Path) -> tuple[Any, ...]:
    entries: list[tuple[Any, ...]] = []
    for path in [*paths, manifest]:
        try:
            stat = path.stat()
        except OSError:
            entries.append((path.name, None))
        else:
            entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def _manifest(path: Path) -> dict[str, dict[str, Any]]:
    """``{filename: entry}``. A missing or broken manifest is an empty one."""
    try:
        raw = path.read_bytes()
    except OSError:
        return {}
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        logger.error("%s is not readable TOML; every template falls back to its own name.", path.name)
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(document.get("template") or []):
        if not isinstance(entry, dict):
            continue
        filename = entry.get("file")
        if not isinstance(filename, str) or not filename:
            logger.warning("%s entry %d names no file; ignored.", path.name, index)
            continue
        entries[filename] = {**entry, "_order": index}
    return entries


def _order(entries: dict[str, dict[str, Any]], filename: str) -> int:
    """Manifest order, with anything unlisted sorted after everything listed."""
    entry = entries.get(filename)
    return entry["_order"] if entry else len(entries)


def _card(path: Path, entry: dict[str, Any]) -> TemplateCard | None:
    if not _SLUG.match(path.stem):
        logger.error("%s cannot be a URL segment, so it is not offered in the gallery.", path.name)
        return None

    try:
        document, issues = library.read_template(path)
    except OSError:
        logger.error("%s could not be read; it is not offered in the gallery.", path.name)
        return None
    if document is None:
        first = issues[0].message if issues else "no reason given"
        logger.error("%s does not validate (%s); it is not offered in the gallery.", path.name, first)
        return None

    flows = document.get("flows") or []
    entry_flow = next((flow for flow in flows if flow.get("key") == document.get("entry")), flows[0] if flows else {})

    try:
        requirements = imports.requirements_for(document)
    except Exception:  # pragma: no cover - a walk that cannot read its own document
        logger.exception("%s could not be walked for requirements; offered without badges.", path.name)
        requirements = []

    platform_labels = dict(Platform.choices)
    platforms = tuple(
        {"key": r.key, "label": str(platform_labels.get(r.key, r.key))} for r in requirements if r.kind == "platform"
    )
    creates = _counted(r for r in requirements if r.creatable and not r.in_document)
    must_have = _counted(r for r in requirements if not r.creatable and not r.optional and r.kind != "platform")

    return TemplateCard(
        slug=path.stem,
        filename=path.name,
        title=str(entry.get("title") or entry_flow.get("name") or path.stem),
        summary=str(entry.get("summary") or ""),
        category=str(entry.get("category") or DEFAULT_CATEGORY),
        flow_count=len(flows),
        node_count=sum(len((flow.get("graph") or {}).get("nodes") or []) for flow in flows),
        trigger_count=sum(len(flow.get("triggers") or []) for flow in flows),
        folder=str(entry_flow.get("folder") or ""),
        platforms=platforms,
        creates=creates,
        must_already_have=must_have,
        calls_out=any(r.kind == "request_header" for r in requirements)
        or any(
            node.get("type") == "external_request"
            for flow in flows
            for node in (flow.get("graph") or {}).get("nodes") or []
        ),
    )


def _counted(requirements: Any) -> tuple[str, ...]:
    """Requirements grouped and counted: ``("2 tags", "1 custom field")``.

    One requirement is one object, so a template that adds two tags raises two
    requirements of the same kind. Listing them ungrouped would read "Adds tag,
    tag to your workspace."
    """
    counts: dict[str, int] = {}
    for requirement in requirements:
        kind = (requirement.kind or "").replace("_", " ")
        counts[kind] = counts.get(kind, 0) + 1
    return tuple(f"{count} {kind}" if count == 1 else f"{count} {kind}s" for kind, count in counts.items())
