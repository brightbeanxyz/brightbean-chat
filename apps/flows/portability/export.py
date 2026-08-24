"""Turning a workspace's flow into a document a stranger can safely import.

Export is a **scrub plus a translation**. The scrub removes what belongs to the
exporting workspace and nothing else: row ids, timestamps, authorship, the
member who gets assigned a conversation, the channel a trigger is bound to, the
credential in a request header, the sending address on an email node, the
account handle in a ref-URL deep link, the platform post ids a comment trigger
watches. The translation replaces every workspace-local id with a synthetic
reference (:mod:`apps.flows.portability.refs`) and records, in ``requirements``,
what each one has to become before the flow will run somewhere else.

--------------------------------------------------------------------------
What "zero workspace-identifying data" means, precisely
--------------------------------------------------------------------------

An exported document carries **no** database id, member identity, connection
identity, credential, signed media URL, workspace name or organization id. It
does carry the author's own content — the flow's name and folder, message text,
keyword lists, a media asset's filename — because that content *is* the
template. ``apps/flows/tests/test_portability_export.py`` asserts the first list
against the serialised bytes, which is why the line is drawn where a test can
stand on it.

Two of those deserve their own sentence:

**A media delivery URL is never exported.** ``apps.media_library.delivery``
mints unguessable, long-lived signed URLs, so putting one in a shared file
hands every reader read access to the exporter's asset for as long as it
exists. A library asset therefore leaves as a reference plus a filename hint,
and the importer supplies its own.

**Request header values are blanked.** A flow author's ``Authorization: Bearer
…`` is a credential (SECURITY-BASELINE §5), and there is no heuristic that
reliably tells one header apart from another — so all of them lose their value
and every one becomes a requirement the import can refill. The names survive,
because the name is the part that says what has to be supplied.

--------------------------------------------------------------------------
Bundles
--------------------------------------------------------------------------

A ``start_flow`` node names another flow, and a sequence a flow subscribes to
runs flows of its own. Exporting one flow of such a set produces a template that
cannot work, so :func:`export_document` can follow those two references to their
closure and put the whole set in one file. The traversal is breadth-first from
the entry flow and bounded by :data:`~apps.flows.portability.envelope.MAX_FLOWS`.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from apps.flows.compat import installed_model
from apps.flows.models import Flow
from apps.flows.portability import refs
from apps.flows.portability.envelope import (
    APP_NAME,
    FORMAT_VERSION,
    MAX_FLOWS,
    REQUIREMENT_KINDS,
    serialize,
)
from apps.flows.schema import SCHEMA_VERSION, empty_graph

__all__ = ["export_document", "export_filename", "flow_closure", "serialize"]

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The requirement table
# --------------------------------------------------------------------------


@dataclass
class _Requirement:
    """One thing the importing workspace has to supply, while it is being built."""

    kind: str
    #: What merges sites onto one entry: a raw id, or a case-folded name.
    identity: str
    name: str = ""
    detail: str = ""
    #: True once a site addresses this object by id, which is what earns it a ref.
    needs_ref: bool = False
    used_by: list[str] = field(default_factory=list)
    ref: str = ""

    def note(self, location: str) -> None:
        if location and location not in self.used_by:
            self.used_by.append(location)


class _Table:
    """First-appearance-ordered requirements, keyed by (kind, identity).

    Ordering is not cosmetic. Synthetic references are minted from an ordinal,
    so "the order objects were first seen" is what makes a second export of an
    imported flow produce the same references as the first — the byte-stable
    round trip the acceptance criterion asks for.
    """

    def __init__(self, names: dict[tuple[str, str], str] | None = None) -> None:
        self._entries: OrderedDict[tuple[str, str], _Requirement] = OrderedDict()
        #: ``(kind, raw id) -> name``, resolved once in pass one and kept so
        #: pass two derives identities exactly the way pass one did. Two copies
        #: of that derivation is how an id-addressed site stops folding onto the
        #: name-addressed entry it belongs to, silently, in one direction only.
        self.names: dict[tuple[str, str], str] = names or {}

    def touch(self, kind: str, identity: str, *, location: str, needs_ref: bool) -> _Requirement:
        key = (kind, identity)
        entry = self._entries.get(key)
        if entry is None:
            entry = _Requirement(kind=kind, identity=identity)
            self._entries[key] = entry
        entry.needs_ref = entry.needs_ref or needs_ref
        entry.note(location)
        return entry

    def get(self, kind: str, identity: str) -> _Requirement | None:
        return self._entries.get((kind, identity))

    def all(self) -> list[_Requirement]:
        return list(self._entries.values())

    def mint(self) -> None:
        """Assign a synthetic reference to every entry addressed by id."""
        counters: dict[str, int] = {}
        for entry in self._entries.values():
            if not entry.needs_ref:
                continue
            counters[entry.kind] = counters.get(entry.kind, 0) + 1
            entry.ref = refs.synthetic_ref(entry.kind, counters[entry.kind])


# --------------------------------------------------------------------------
# Identity: how a site names the thing it points at
# --------------------------------------------------------------------------


def _fold(value: Any) -> str:
    """A name reduced to what matters for "is this the same object".

    Case-folded and trimmed because that is how the product already compares
    them: ``get_or_create_tag`` and ``custom_field_by_name`` are both
    ``name__iexact``, so "VIP" and "vip" are one tag and must be one
    requirement.
    """
    return value.strip().casefold() if isinstance(value, str) else ""


def _site_identity(site: refs.Site, names: dict[tuple[str, str], str]) -> str | None:
    """What this site's requirement is keyed on, or ``None`` if it is not one.

    An id-addressed site is keyed on the object's **name** when the object was
    found and the kind can also be addressed by name — which is what collapses
    ``add_tag: "VIP"`` and a condition rule naming the same tag's id onto a
    single question in the mapping step. Everything else is keyed on the raw
    value, which for a member or a media asset is all there is.
    """
    if site.addressing == refs.ADDRESS_NAME and site.kind in refs.NAMED_KINDS:
        folded = _fold(site.value)
        return folded or None

    if site.kind in refs.RESOLVED_KINDS:
        raw = str(site.value) if site.value not in (None, "") else ""
        if not raw:
            return None
        if site.kind in refs.NAMED_KINDS:
            resolved = names.get((site.kind, raw))
            if resolved:
                return _fold(resolved)
        return raw

    return None


# --------------------------------------------------------------------------
# Resolving ids to names, one query per kind
# --------------------------------------------------------------------------


def _lookup_names(workspace: Any, wanted: dict[str, set[str]]) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """``(kind, raw id) -> name`` and ``(kind, raw id) -> detail``, scoped.

    Every query goes through ``for_workspace``: an id sitting in a graph is
    author-editable text, and resolving one outside the workspace that owns the
    flow would put another tenant's tag name into a file somebody is about to
    share (SECURITY-BASELINE §1). An id that resolves to nothing simply has no
    name, which the manifest reports honestly as a reference with no label.

    A member is deliberately absent from the result. Its name and email identify
    a person, so a member reference travels as an ordinal and nothing else.
    """
    names: dict[tuple[str, str], str] = {}
    details: dict[str, str] = {}

    def rows(kind: str, model: Any, fields: tuple[str, ...]) -> None:
        ids = _uuids(wanted.get(kind, set()))
        if model is None or not ids:
            return
        for row in model.objects.for_workspace(workspace).filter(pk__in=ids).values("id", *fields):
            names[(kind, str(row["id"]))] = str(row[fields[0]])
            if len(fields) > 1:
                details[str(row["id"])] = str(row[fields[1]])

    rows(refs.KIND_TAG, installed_model("contacts", "apps.contacts", "Tag"), ("name",))
    rows(refs.KIND_CUSTOM_FIELD, installed_model("contacts", "apps.contacts", "CustomField"), ("name", "type"))
    rows(refs.KIND_SEGMENT, installed_model("contacts", "apps.contacts", "Segment"), ("name",))
    rows(refs.KIND_SEQUENCE, installed_model("campaigns", "apps.campaigns", "Sequence"), ("name",))
    rows(refs.KIND_MEDIA, installed_model("media_library", "apps.media_library", "MediaAsset"), ("filename", "kind"))
    rows(refs.KIND_FLOW, Flow, ("name",))
    return names, details


def _uuids(values: set[str]) -> list[UUID]:
    """The subset of ``values`` that are UUIDs, so a hand-edited graph cannot 500."""
    parsed = []
    for value in values:
        try:
            parsed.append(UUID(value))
        except (ValueError, AttributeError, TypeError):
            continue
    return parsed


# --------------------------------------------------------------------------
# The closure
# --------------------------------------------------------------------------


def flow_closure(flow: Flow) -> list[Flow]:
    """``flow`` and every flow it needs, breadth-first, deduplicated.

    Two edges are followed. ``start_flow`` names its target directly (SPEC
    §11.3). A ``subscribe_sequence`` verb names a sequence, and a sequence is a
    ladder of steps each of which starts a flow (SPEC §12) — so a template whose
    welcome flow subscribes somebody to onboarding is incomplete without
    onboarding's flows.

    The sequence itself is **not** exported: it is a workspace object with its
    own schedule and enrollment state, and it appears in ``requirements`` as
    something to create or map. What travels is the flows its steps run.
    """
    from apps.flows.services import latest_version

    found: OrderedDict[Any, Flow] = OrderedDict({flow.pk: flow})
    queue = [flow]
    while queue and len(found) < MAX_FLOWS:
        current = queue.pop(0)
        version = latest_version(current)
        graph = version.graph_json if version else empty_graph()
        for target in _referenced_flows(current.workspace_id, graph):
            if target.pk not in found and len(found) < MAX_FLOWS:
                found[target.pk] = target
                queue.append(target)
    return list(found.values())


def _referenced_flows(workspace_id: Any, graph: Any) -> list[Flow]:
    """The flows one graph reaches, in walk order."""
    flow_ids: list[str] = []
    sequence_ids: list[str] = []

    def collect(site: refs.Site) -> Any:
        raw = str(site.value) if isinstance(site.value, str) else ""
        if site.kind == refs.KIND_FLOW and raw:
            flow_ids.append(raw)
        elif site.kind == refs.KIND_SEQUENCE and site.addressing == refs.ADDRESS_ID and raw:
            sequence_ids.append(raw)
        return site.value

    refs.rewrite_graph(graph, collect)

    for sequence_id in sequence_ids:
        flow_ids.extend(_sequence_step_flow_ids(workspace_id, sequence_id))

    if not flow_ids:
        return []
    by_id = {str(row.pk): row for row in Flow.objects.for_workspace(workspace_id).filter(pk__in=_uuids(set(flow_ids)))}
    # Walk order, not query order: a bundle exported twice has to list its flows
    # in the same sequence or the round trip is not byte-stable.
    ordered: list[Flow] = []
    for flow_id in flow_ids:
        target = by_id.get(flow_id)
        if target is not None and target not in ordered:
            ordered.append(target)
    return ordered


def _sequence_step_flow_ids(workspace_id: Any, sequence_id: str) -> list[str]:
    """The flows a sequence's steps start, in step order."""
    model = installed_model("campaigns", "apps.campaigns", "SequenceStep")
    if model is None:
        return []
    parsed = _uuids({sequence_id})
    if not parsed:
        return []
    return [
        str(row["flow_id"])
        for row in model.objects.for_workspace(workspace_id)
        .filter(sequence_id=parsed[0])
        .order_by("position")
        .values("flow_id")
    ]


# --------------------------------------------------------------------------
# The export itself
# --------------------------------------------------------------------------


def export_document(flow: Flow, *, bundle: bool = False) -> dict[str, Any]:
    """One flow — or its whole closure — as a portable document.

    Two walks over the same content. The first collects every reference site
    and resolves the ids it finds to names, so the requirement table is complete
    before any substitution happens; the second rewrites the documents against
    that table. One walk would have to decide a site's requirement entry before
    knowing whether a later site names the same object, and "the tag added by
    name" and "the tag matched by id" would land in two separate questions in
    the mapping step.
    """
    from apps.flows.services import latest_version
    from apps.flows.triggers.services import triggers_for

    flows = flow_closure(flow) if bundle else [flow]
    keys = {row.pk: f"flow-{index + 1}" for index, row in enumerate(flows)}

    sources: list[tuple[Flow, str, Any, list[Any]]] = []
    for row in flows:
        version = latest_version(row)
        graph = version.graph_json if version else empty_graph()
        sources.append((row, keys[row.pk], graph, list(triggers_for(row))))

    table = _collect(flow.workspace_id, sources)
    _mark_in_document(table, keys)
    exported = [
        {
            "key": key,
            "name": row.name,
            "folder": row.folder,
            "graph": _rewrite_graph(graph, table, key),
            "triggers": [_export_trigger(trigger, table, key, index) for index, trigger in enumerate(triggers)],
        }
        for row, key, graph, triggers in sources
    ]

    return {
        "app": APP_NAME,
        "format": FORMAT_VERSION,
        "schema": SCHEMA_VERSION,
        "entry": keys[flow.pk],
        "flows": exported,
        "requirements": _manifest(table),
    }


def _mark_in_document(table: _Table, keys: dict[Any, str]) -> None:
    """Tell a flow requirement which flow in this document satisfies it.

    A ``start_flow`` pointing at a flow the bundle also carries needs no answer
    from the importer at all — it resolves to the copy that is about to be
    created. Recording the document key is what lets the import wizard say so
    instead of asking a question whose only correct answer is "that one".
    """
    by_id = {str(pk): key for pk, key in keys.items()}
    for entry in table.all():
        if entry.kind == refs.KIND_FLOW and entry.identity in by_id:
            entry.detail = by_id[entry.identity]


def _collect(workspace_id: Any, sources: list[tuple[Flow, str, Any, list[Any]]]) -> _Table:
    """Pass one: every site, then the names behind the ids, then the table."""
    sites: list[tuple[refs.Site, str]] = []

    platforms: list[tuple[str, str]] = []
    for _row, key, graph, triggers in sources:
        refs.rewrite_graph(graph, _recorder(sites, key))
        for index, trigger in enumerate(triggers):
            refs.rewrite_trigger_config(
                trigger.type, trigger.config_json or {}, _recorder(sites, key, f"trigger-{index}")
            )
            # A trigger's connection is a model field rather than something in
            # its config, so it is not a reference site — but the *platform* it
            # exports as is still something the importing workspace has to
            # supply, and a manifest that did not name it would be a template
            # whose channel requirements you only discover in the wizard.
            connection = trigger.channel_connection
            if connection is not None:
                platforms.append((str(connection.platform), f"{key}:trigger-{index}"))

    wanted: dict[str, set[str]] = {}
    for site, _location in sites:
        if site.addressing == refs.ADDRESS_ID and site.kind in refs.RESOLVED_KINDS and isinstance(site.value, str):
            wanted.setdefault(site.kind, set()).add(site.value)
    names, details = _lookup_names(workspace_id, wanted)

    table = _Table(names)
    for site, location in sites:
        _record(table, site, location, names, details)
    for platform, location in platforms:
        table.touch("platform", platform, location=location, needs_ref=False).name = platform
    table.mint()
    return table


def _recorder(sink: list[tuple[refs.Site, str]], flow_key: str, location: str | None = None):
    """A ``visit`` that changes nothing and remembers everything."""

    def visit(site: refs.Site) -> Any:
        where = location if location is not None else (site.node_id or "graph")
        sink.append((site, f"{flow_key}:{where}"))
        return site.value

    return visit


def _record(
    table: _Table,
    site: refs.Site,
    location: str,
    names: dict[tuple[str, str], str],
    details: dict[str, str],
) -> None:
    """Fold one site into the requirement table."""
    if site.kind in refs.STRIPPED_KINDS:
        # ``detail`` is the label for these: the header's name, the WhatsApp
        # template's <name>/<language>. It goes into ``name`` because that is
        # the manifest's label field and the wizard's question heading — the
        # *value* is what was removed, so the name is all there is left to show.
        entry = table.touch(site.kind, _stripped_identity(site, location), location=location, needs_ref=False)
        if site.detail and not entry.name:
            entry.name = site.detail
        return

    identity = _site_identity(site, names)
    if identity is None:
        return
    entry = table.touch(
        site.kind,
        identity,
        location=location,
        needs_ref=site.addressing == refs.ADDRESS_ID,
    )

    if not entry.name:
        if site.addressing == refs.ADDRESS_NAME:
            entry.name = str(site.value).strip()
        elif site.kind != refs.KIND_MEMBER:
            # A member is the one kind whose name is a person's, so it is never
            # written down; every other kind's name is a label the importer
            # needs in order to answer the mapping question at all.
            entry.name = names.get((site.kind, str(site.value)), "")
    if not entry.detail and site.kind in (refs.KIND_MEDIA, refs.KIND_CUSTOM_FIELD):
        entry.detail = details.get(str(site.value), "")


def _stripped_identity(site: refs.Site, location: str) -> str:
    """What merges two stripped sites into one requirement.

    A request header is per node and per header name — refilling one is a
    different answer from refilling another. A WhatsApp template is per
    ``<name>/<language>``, because that reference is the same template wherever
    it is used. The rest are per site.
    """
    if site.kind == refs.KIND_REQUEST_HEADER:
        return f"{location}:{site.detail}"
    if site.kind == refs.KIND_WHATSAPP_TEMPLATE:
        return site.detail or location
    return location


def _manifest(table: _Table) -> dict[str, list[dict[str, Any]]]:
    """The ``requirements`` block: every kind present, empty ones included."""
    manifest: dict[str, list[dict[str, Any]]] = {kind: [] for kind in REQUIREMENT_KINDS}
    for entry in table.all():
        row: dict[str, Any] = {"key": entry.ref or entry.identity}
        if entry.ref:
            row["ref"] = entry.ref
        if entry.name:
            row["name"] = entry.name
        if entry.detail:
            row["detail"] = entry.detail
        row["used_by"] = list(entry.used_by)
        manifest.setdefault(entry.kind, []).append(row)
    return manifest


# --------------------------------------------------------------------------
# Pass two: substitution
# --------------------------------------------------------------------------


def _substitute(table: _Table, flow_key: str, location: str | None = None):
    """A ``visit`` that replaces each site with what the table says it becomes."""

    def visit(site: refs.Site) -> Any:
        if site.kind == refs.KIND_REQUEST_HEADER:
            # The name stays, the value goes. See the module docstring.
            return ""
        if site.kind == refs.KIND_WHATSAPP_TEMPLATE:
            return refs.REMOVE
        if site.kind == refs.KIND_COMMENT_POSTS:
            # An **empty list**, not a removed key. The post ids belong to the
            # exporter's account, but the fact that the trigger watches specific
            # posts is part of the template — and a removed key would leave the
            # importer's own walk with nothing to raise a question about, so
            # ``post_scope: specific`` would arrive silently watching nothing.
            return []
        if site.kind in (refs.KIND_LINK_HANDLE, refs.KIND_FROM_OVERRIDE):
            return ""
        if site.addressing == refs.ADDRESS_NAME:
            # A name is already portable; it travels as the author wrote it.
            return site.value

        identity = _site_identity(site, table.names)
        entry = table.get(site.kind, identity) if identity else None
        if entry is None or not entry.ref:
            # Only reachable for a site the collection pass declined to record —
            # a blank or non-string id. Leaving it alone keeps the document
            # honest: the validator will say what is wrong with it.
            where = location if location is not None else (site.node_id or "graph")
            logger.debug("Export: no requirement for %s at %s:%s", site.kind, flow_key, where)
            return site.value
        return entry.ref

    return visit


def _rewrite_graph(graph: Any, table: _Table, flow_key: str) -> Any:
    return refs.rewrite_graph(graph, _substitute(table, flow_key))


def _export_trigger(trigger: Any, table: _Table, flow_key: str, index: int) -> dict[str, Any]:
    """One trigger, without its connection and without its priority.

    ``platform`` rather than the connection id: SPEC §5 makes a null connection
    mean "all connections of a matching platform", so the platform is the part
    that is portable and the id is the part that is not. ``priority`` is
    workspace-wide (``apps.flows.triggers.services.workspace_triggers`` explains
    why), so it means nothing in another workspace; imported triggers land at
    the end of the target's order in document order.

    ``enabled`` is not exported either. Every imported trigger arrives disabled,
    so carrying the source's flag would only look like a promise this makes no
    attempt to keep.
    """
    connection = trigger.channel_connection
    return {
        "type": trigger.type,
        "platform": connection.platform if connection is not None else None,
        "config": refs.rewrite_trigger_config(
            trigger.type, trigger.config_json or {}, _substitute(table, flow_key, f"trigger-{index}")
        ),
    }


def export_filename(flow: Flow, *, bundle: bool = False) -> str:
    """A safe download filename derived from the flow's name.

    The name is author text, so it is reduced to an ASCII slug rather than
    quoted: a filename reaches a ``Content-Disposition`` header, and a header is
    the wrong place to discover that somebody put a newline in a flow name.
    """
    slug = "".join(character if character.isalnum() else "-" for character in flow.name.lower())
    slug = "-".join(part for part in slug.split("-") if part)[:60] or "flow"
    return f"{slug}{'-bundle' if bundle else ''}.flow.json"
