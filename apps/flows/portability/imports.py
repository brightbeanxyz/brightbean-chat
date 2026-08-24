"""Reading a shared template: validate, map, dry-run, then create.

**An import is untrusted input.** A template arrives from a stranger, so this
module is written so that nothing reaches the ORM until the document has been
through the validators the product already trusts, and nothing is *created*
until a person has answered every question the document raises and confirmed a
summary of what will happen.

The order is fixed and each step refuses the next:

1. :func:`~apps.flows.portability.envelope.parse` — size cap, then parse, then
   depth, then a NUL scan. A hostile file costs a length check, not a walk.
2. :func:`~apps.flows.portability.envelope.validate_envelope` — the envelope's
   shape with unknown keys rejected, the two version stamps, then every graph
   through ``apps.flows.schema.validate_graph`` (which is where
   ``MAX_NODES``/``MAX_EDGES``/size/depth are enforced) and every trigger config
   through ``apps.flows.triggers.validation.validate_config``.
3. :func:`requirements_for` — **re-derived from the document**, never read out
   of its ``requirements`` block. A manifest that omits a reference cannot
   therefore talk this module into importing a dangling id; the manifest
   supplies labels and defaults and no authority at all.
4. :func:`plan_import` — the dry run. What would be created, what is still
   unanswered, and every URL an ``external_request`` node would call.
5. :func:`apply_import` — one transaction, and the first write of the whole
   flow.

--------------------------------------------------------------------------
What arrives, and what does not
--------------------------------------------------------------------------

An imported flow is a **draft**: ``create_flow`` then ``save_draft``, so it is
version 1, unpublished, ``status=draft``. Publishing stays a human action —
nothing here sets ``published``. Imported triggers are created ``enabled=False``
for the same reason: a template that starts answering a workspace's customers
the moment it is uploaded is not something a stranger's file should be able to
arrange.

``save_draft`` is also deliberately the write path rather than a hand-built
``FlowVersion``. It runs ``sanitize_graph``, so an imported ``send_email``
``html_body`` goes through the same markup allowlist every other write does —
which is the difference between storing a stranger's HTML and storing markup
that will execute in a member's browser when they open the builder.

Nothing here builds a Django ``Template`` from imported text, and nothing may.
``{{placeholders}}`` in an imported message are rendered by
``apps.flows.rendering``, which is plain token substitution with no template
engine to reach (SECURITY-BASELINE §3).
"""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db import transaction

from apps.flows.compat import installed_model
from apps.flows.models import Flow
from apps.flows.portability import refs
from apps.flows.portability.envelope import REQUIREMENT_KINDS, parse, validate_envelope
from apps.flows.schema.issues import Issue

__all__ = [
    "ACTION_BLANK",
    "ACTION_CREATE",
    "ACTION_KEEP",
    "ACTION_MAP",
    "ACTION_SKIP",
    "CREATABLE_KINDS",
    "OPTIONAL_KINDS",
    "ImportNotReadyError",
    "ImportPlan",
    "Requirement",
    "Resolution",
    "TRIGGER_KIND",
    "TriggerChoice",
    "apply_import",
    "default_mapping",
    "outbound_requests",
    "parse_and_validate",
    "plan_import",
    "requirements_for",
    "trigger_choices",
]

logger = logging.getLogger(__name__)

#: The three answers a requirement can be given. ``create`` makes the object,
#: ``map`` points at one that already exists, and ``blank`` is the deliberate
#: "leave it out" available only where leaving it out is a legal document
#: (an unbound trigger, an empty request header).
ACTION_CREATE = "create"
ACTION_MAP = "map"
ACTION_BLANK = "blank"

#: Triggers are not a *requirement* — nothing has to be supplied for one — but
#: the issue asks for them to be skippable, and "this template brings a keyword
#: trigger you may not want" is a real question. They ride in the same mapping
#: dictionary under this pseudo-kind so the wizard has one form and one parser.
TRIGGER_KIND = "trigger"
ACTION_KEEP = "keep"
ACTION_SKIP = "skip"

#: Kinds the mapping step can create from nothing.
#:
#: A **segment** is absent on purpose: it is a saved *filter*, and inventing one
#: that matches nobody would silently change what an imported condition means.
#: A **media asset** is absent because bytes cannot be conjured — a media
#: requirement is answered with a library asset or with a URL instead.
#:
#: A **flow** is here, and it is the interesting one. A ``start_flow`` node
#: whose target the file does not carry — a single-flow export of a flow that
#: hands over to another — has no correct id to resolve to, and leaving it
#: pointing at the exporter's id is precisely the dangling reference this whole
#: module exists to prevent. So the mapping step offers to create an empty draft
#: under the name the template expected: a real flow in this workspace, visibly
#: unfinished, named after what belongs there. The dry run says so, and exporting
#: the **bundle** instead is the way to avoid the placeholder altogether.
CREATABLE_KINDS: frozenset[str] = frozenset({refs.KIND_TAG, refs.KIND_CUSTOM_FIELD, refs.KIND_SEQUENCE, refs.KIND_FLOW})

#: Kinds an import may leave unanswered because the resulting document is still
#: valid and still honest about what it is missing.
OPTIONAL_KINDS: frozenset[str] = frozenset(
    {
        "platform",
        refs.KIND_REQUEST_HEADER,
        refs.KIND_WHATSAPP_TEMPLATE,
        refs.KIND_LINK_HANDLE,
        refs.KIND_FROM_OVERRIDE,
        refs.KIND_COMMENT_POSTS,
    }
)


@dataclass
class Requirement:
    """One question the mapping step asks, derived from the document itself."""

    kind: str
    #: Identifies the requirement within its kind, and keys the mapping.
    key: str
    #: The synthetic reference the graphs use, when anything addresses it by id.
    ref: str = ""
    #: What the exporting workspace called it. A label and a default — the
    #: manifest is the only place this can come from and it is not trusted for
    #: anything else.
    name: str = ""
    detail: str = ""
    used_by: list[str] = field(default_factory=list)
    #: True when this reference is satisfied by the document itself: a
    #: ``start_flow`` pointing at another flow in the same bundle.
    in_document: str = ""

    @property
    def creatable(self) -> bool:
        return self.kind in CREATABLE_KINDS

    @property
    def optional(self) -> bool:
        return self.kind in OPTIONAL_KINDS or bool(self.in_document)


@dataclass
class Resolution:
    """What one requirement was answered with, once the answer is checked."""

    requirement: Requirement
    action: str = ""
    #: The object it resolves to, when it resolves to one.
    target_id: str = ""
    #: The final name — for a create, and for the name-addressed sites.
    name: str = ""
    #: A literal the graph should carry instead of an id: a media URL, a header
    #: value, a comma-separated post id list.
    literal: str = ""
    problem: str = ""

    @property
    def answered(self) -> bool:
        return not self.problem


@dataclass
class TriggerChoice:
    """One trigger in the document, and whether it is being kept.

    Kept by default. Every imported trigger arrives **disabled** whatever this
    says (see :func:`apply_import`), so the choice is between "disabled and
    there to switch on" and "not imported at all" — never between off and live.
    """

    flow_key: str
    flow_name: str
    index: int
    type: str
    label: str
    platform: str | None
    keep: bool = True

    @property
    def key(self) -> str:
        return f"{self.flow_key}:trigger-{self.index}"


@dataclass
class ImportPlan:
    """The dry run: everything a person needs before they press the button."""

    document: dict[str, Any]
    resolutions: list[Resolution]
    #: ``[{flow_key, name, node_id, method, url}]`` — every outbound call an
    #: imported flow would make. Surfaced because the URL was chosen by whoever
    #: wrote the template, not by whoever is importing it.
    outbound_requests: list[dict[str, str]] = field(default_factory=list)
    #: Findings that do not stop the import: a half-wired graph, a manifest that
    #: disagrees with the document.
    notes: list[str] = field(default_factory=list)
    #: Every trigger the document carries, and whether it is being kept.
    triggers: list[TriggerChoice] = field(default_factory=list)

    @property
    def unanswered(self) -> list[Resolution]:
        return [resolution for resolution in self.resolutions if not resolution.answered]

    @property
    def can_apply(self) -> bool:
        return not self.unanswered

    @property
    def flow_names(self) -> list[str]:
        return [flow["name"] for flow in self.document["flows"]]


# --------------------------------------------------------------------------
# Step 1 and 2: parse and validate
# --------------------------------------------------------------------------


def parse_and_validate(raw: bytes | str) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Bytes to a document this server is willing to look at, or the findings."""
    document, issues = parse(raw)
    if document is None:
        return None, issues
    issues = validate_envelope(document)
    if issues:
        return None, issues
    return document, []


# --------------------------------------------------------------------------
# Step 3: requirements, re-derived
# --------------------------------------------------------------------------


def requirements_for(document: dict[str, Any]) -> list[Requirement]:
    """Every question this document raises, found by walking it.

    The **set** comes from the walk. The **labels** come from the document's own
    ``requirements`` block, which is why a lying manifest is harmless: a
    reference it forgot still produces a requirement (with no label), and one it
    invented for a reference nothing uses is simply never asked about.

    One label does more than decorate. A tag can be reached twice in one flow —
    ``add_tag`` names it, a condition rule matches its id — and those are one
    question, not two. The exporter folded them onto a single entry keyed by the
    tag's folded name, so when the manifest gives a name for a synthetic
    reference this re-creates that fold. When it does not, the reference gets a
    question of its own: less tidy, never wrong.
    """
    labels = _labels(document)
    in_document = {flow["key"] for flow in document["flows"]}
    found: dict[tuple[str, str], Requirement] = {}
    order: list[tuple[str, str]] = []

    def record(kind: str, key: str, location: str, *, ref: str = "", name: str = "") -> Requirement:
        identity = (kind, key)
        requirement = found.get(identity)
        if requirement is None:
            label = labels.get((kind, ref or key)) or labels.get((kind, key)) or {}
            requirement = Requirement(
                kind=kind,
                key=key,
                ref=ref,
                name=name or str(label.get("name") or ""),
                detail=str(label.get("detail") or ""),
            )
            if kind == refs.KIND_FLOW and requirement.detail in in_document:
                requirement.in_document = requirement.detail
            found[identity] = requirement
            order.append(identity)
        if ref and not requirement.ref:
            requirement.ref = ref
        if location not in requirement.used_by:
            requirement.used_by.append(location)
        return requirement

    for flow in document["flows"]:
        key = flow["key"]
        refs.rewrite_graph(flow["graph"], _collector(record, labels, key))
        for index, trigger in enumerate(flow["triggers"]):
            refs.rewrite_trigger_config(
                trigger["type"], trigger["config"], _collector(record, labels, key, f"trigger-{index}")
            )
            platform = trigger.get("platform")
            if isinstance(platform, str) and platform:
                record("platform", platform, f"{key}:trigger-{index}", name=platform)

    return [found[identity] for identity in order]


def _collector(record: Any, labels: dict[tuple[str, str], dict[str, Any]], flow_key: str, location: str | None = None):
    """A ``visit`` that records a requirement per reference and changes nothing."""

    def visit(site: refs.Site) -> Any:
        where = f"{flow_key}:{location if location is not None else (site.node_id or 'graph')}"

        if site.kind in refs.STRIPPED_KINDS:
            if site.kind == refs.KIND_REQUEST_HEADER:
                # Keyed the way the exporter keyed it — per node, per header
                # name — so the manifest's label lands on the right question.
                record(site.kind, f"{where}:{site.detail or ''}", where, name=site.detail or "")
            elif site.kind == refs.KIND_WHATSAPP_TEMPLATE:
                # Keyed on <name>/<language>, which is the same template
                # wherever it is used and the only part of it that survives the
                # export. Nothing has to be answered — it is here so the summary
                # can say which approved template the workspace will need.
                if site.detail:
                    record(site.kind, site.detail, where, name=site.detail)
            elif site.kind in (refs.KIND_LINK_HANDLE, refs.KIND_FROM_OVERRIDE, refs.KIND_COMMENT_POSTS):
                record(site.kind, where, where)
            return site.value

        if site.addressing == refs.ADDRESS_NAME:
            name = str(site.value).strip() if isinstance(site.value, str) else ""
            if name and site.kind in refs.NAMED_KINDS:
                record(site.kind, name.casefold(), where, name=name)
            return site.value

        raw = str(site.value) if isinstance(site.value, str) and site.value else ""
        if not raw:
            return site.value
        label = labels.get((site.kind, raw)) or {}
        name = str(label.get("name") or "")
        key = name.casefold() if name and site.kind in refs.NAMED_KINDS else raw
        record(site.kind, key, where, ref=raw, name=name)
        return site.value

    return visit


def _labels(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """The manifest, indexed for lookup. Advisory data, treated as such.

    Indexed by ``ref`` *and* by ``key``, because an id-addressed site knows only
    the synthetic reference it holds while a name-addressed one knows only the
    name — and the exporter keys tag and custom-field entries by the folded name
    so the two collapse onto one question.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    manifest = document.get("requirements")
    if not isinstance(manifest, dict):
        return index
    for kind, entries in manifest.items():
        if kind not in REQUIREMENT_KINDS or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for candidate in (entry.get("ref"), entry.get("key")):
                if isinstance(candidate, str) and candidate:
                    index.setdefault((kind, candidate), entry)
            name = entry.get("name")
            if isinstance(name, str) and name:
                index.setdefault((kind, name.casefold()), entry)
    return index


# --------------------------------------------------------------------------
# Step 4: the dry run
# --------------------------------------------------------------------------


def default_mapping(workspace: Any, document: dict[str, Any], *, user: Any = None) -> dict[str, dict[str, Any]]:
    """The answers to start the mapping form with.

    Chosen to be the least surprising thing that is also never wrong: map to an
    object of the same name where one exists, offer to create it where the kind
    can be created, and default a member to the person doing the import — who is
    demonstrably in the workspace, which is the property that matters.
    """
    mapping: dict[str, dict[str, Any]] = {}
    for requirement in requirements_for(document):
        existing = _find_by_name(workspace, requirement)
        answer: dict[str, Any] = {}
        if requirement.in_document or requirement.kind in refs.STRIPPED_KINDS:
            answer = {"action": ACTION_BLANK}
        elif existing is not None:
            answer = {"action": ACTION_MAP, "id": str(existing)}
        elif requirement.kind == refs.KIND_MEMBER and user is not None and getattr(user, "pk", None):
            answer = {"action": ACTION_MAP, "id": str(user.pk)}
        elif requirement.creatable:
            answer = {"action": ACTION_CREATE, "name": requirement.name}
            if requirement.kind == refs.KIND_CUSTOM_FIELD:
                answer["field_type"] = requirement.detail or "text"
        elif requirement.optional:
            answer = {"action": ACTION_BLANK}
        mapping.setdefault(requirement.kind, {})[requirement.key] = answer
    return mapping


def plan_import(workspace: Any, document: dict[str, Any], mapping: dict[str, Any] | None = None) -> ImportPlan:
    """Check every answer against the target workspace. Reads only.

    Every lookup goes through ``for_workspace``: a mapping is form input, and an
    id typed into it must not be able to name another tenant's tag
    (SECURITY-BASELINE §1). An id that is not in this workspace is simply not
    found, which surfaces as an unanswered requirement rather than a leak.
    """
    mapping = mapping or {}
    resolutions = [
        _resolve(workspace, requirement, (mapping.get(requirement.kind) or {}).get(requirement.key) or {})
        for requirement in requirements_for(document)
    ]
    return ImportPlan(
        document=document,
        resolutions=resolutions,
        outbound_requests=outbound_requests(document),
        notes=_notes(document),
        triggers=trigger_choices(document, mapping),
    )


def trigger_choices(document: dict[str, Any], mapping: dict[str, Any] | None = None) -> list[TriggerChoice]:
    """Every trigger in the document, in order, with the keep/skip answer applied.

    Kept unless the mapping says otherwise: a template's triggers are the half of
    it that says *when* the flow runs, so dropping them by default would import
    something that can never start. They arrive disabled either way.
    """
    from apps.flows.triggers.registry import spec_for

    answers = (mapping or {}).get(TRIGGER_KIND) or {}
    choices: list[TriggerChoice] = []
    for flow in document["flows"]:
        for index, trigger in enumerate(flow["triggers"]):
            spec = spec_for(str(trigger["type"]))
            choice = TriggerChoice(
                flow_key=str(flow["key"]),
                flow_name=str(flow["name"]),
                index=index,
                type=str(trigger["type"]),
                label=spec.label if spec is not None else str(trigger["type"]),
                platform=trigger.get("platform"),
            )
            answer = answers.get(choice.key) or {}
            choice.keep = str(answer.get("action") or ACTION_KEEP) != ACTION_SKIP
            choices.append(choice)
    return choices


def outbound_requests(document: dict[str, Any]) -> list[dict[str, str]]:
    """Every URL an imported flow would call, with the node that would call it.

    Surfaced before the import can be confirmed because the address was chosen
    by whoever wrote the template. Nothing here fetches any of them, and nothing
    ever will from an import path: at send time the External Request node goes
    through ``apps.common.outbound.guarded_request``, which refuses private and
    link-local addresses, pins the resolved address and re-validates redirects
    (SECURITY-BASELINE §6).
    """
    found: list[dict[str, str]] = []
    for flow in document["flows"]:
        nodes = flow["graph"].get("nodes") if isinstance(flow["graph"], dict) else None
        for node in nodes or []:
            if not isinstance(node, dict) or node.get("type") != "external_request":
                continue
            config = node.get("config")
            if not isinstance(config, dict):
                continue
            found.append(
                {
                    "flow_key": str(flow["key"]),
                    "flow_name": str(flow["name"]),
                    "node_id": str(node.get("id") or ""),
                    "method": str(config.get("method") or ""),
                    "url": str(config.get("url") or ""),
                }
            )
    return found


def _notes(document: dict[str, Any]) -> list[str]:
    """Non-blocking findings, including where the manifest disagrees with the file.

    Graph errors are notes rather than refusals. A dangling edge or a missing
    entry node is an ordinary half-wired draft — SPEC §16's autosave stores them
    every two seconds — so refusing to import one would be stricter than the
    builder that produced it. They still block publish, as they always do.
    """
    from apps.flows.schema import validate_graph

    notes: list[str] = []
    for flow in document["flows"]:
        for issue in validate_graph(flow["graph"]).graph_errors:
            notes.append(f"{flow['name']}: {issue.message} It imports as a draft and cannot be published until fixed.")

    requirements = requirements_for(document)
    derived = {(requirement.kind, requirement.key) for requirement in requirements}
    derived |= {(requirement.kind, requirement.ref) for requirement in requirements if requirement.ref}
    declared = {
        (kind, str(entry.get("key")))
        for kind, entries in (document.get("requirements") or {}).items()
        if isinstance(entries, list)
        for entry in entries
        if isinstance(entry, dict)
    }
    stale = declared - derived
    if stale:
        notes.append(
            f"The file's manifest lists {len(stale)} requirement(s) that nothing in it actually references. "
            f"They are ignored: what has to be supplied is worked out from the flows themselves."
        )
    return notes


# --------------------------------------------------------------------------
# Resolving one answer
# --------------------------------------------------------------------------

_MODELS: dict[str, tuple[str, str, str]] = {
    refs.KIND_TAG: ("contacts", "apps.contacts", "Tag"),
    refs.KIND_CUSTOM_FIELD: ("contacts", "apps.contacts", "CustomField"),
    refs.KIND_SEGMENT: ("contacts", "apps.contacts", "Segment"),
    refs.KIND_SEQUENCE: ("campaigns", "apps.campaigns", "Sequence"),
    refs.KIND_MEDIA: ("media_library", "apps.media_library", "MediaAsset"),
}

#: What to call each kind when telling somebody the answer is missing.
_NOUNS: dict[str, str] = {
    refs.KIND_TAG: "tag",
    refs.KIND_CUSTOM_FIELD: "custom field",
    refs.KIND_SEQUENCE: "sequence",
    refs.KIND_SEGMENT: "segment",
    refs.KIND_MEMBER: "member",
    refs.KIND_FLOW: "flow",
    refs.KIND_MEDIA: "media asset",
    "platform": "channel connection",
}


def _resolve(workspace: Any, requirement: Requirement, answer: dict[str, Any]) -> Resolution:
    resolution = Resolution(requirement=requirement, action=str(answer.get("action") or ""))
    noun = _NOUNS.get(requirement.kind, requirement.kind)

    if requirement.in_document:
        # A start_flow pointing at another flow in the same bundle. It resolves
        # to the copy about to be created, so there is nothing to ask.
        resolution.action = ACTION_MAP
        return resolution

    if requirement.kind in refs.STRIPPED_KINDS:
        # These were not translated, they were **removed** — a credential, an
        # account handle, the exporter's own post ids. There is nothing to map
        # to and nothing to create; there is only a value to supply or not.
        resolution.action = ACTION_BLANK
        resolution.literal = str(answer.get("value") or "")
        return resolution

    if resolution.action == ACTION_BLANK:
        if not requirement.optional:
            resolution.problem = f"This {noun} has to be supplied — the flow cannot run without it."
            return resolution
        resolution.literal = str(answer.get("value") or "")
        return resolution

    if resolution.action == ACTION_CREATE:
        if not requirement.creatable:
            resolution.problem = f"A {noun} cannot be created from a template; pick an existing one."
            return resolution
        return _resolve_create(workspace, resolution, requirement, answer, noun)

    if resolution.action == ACTION_MAP:
        target = _existing(workspace, requirement, answer.get("id"))
        if target is None:
            if requirement.kind == refs.KIND_MEDIA and str(answer.get("url") or "").strip():
                resolution.literal = str(answer["url"]).strip()[:2000]
                return resolution
            if requirement.optional:
                # "Every connection of a matching platform" is a real answer
                # (SPEC §5), and it is what an unfilled channel picker means.
                return resolution
            resolution.problem = f"Pick a {noun} in this workspace."
            return resolution
        resolution.target_id = str(target[0])
        resolution.name = str(target[1])
        return resolution

    resolution.problem = f"Choose what this {noun} should become."
    return resolution


def _resolve_create(
    workspace: Any, resolution: Resolution, requirement: Requirement, answer: dict[str, Any], noun: str
) -> Resolution:
    """Check a "create it" answer the way the creating service will.

    Through ``apps.common.naming.clean_name`` — the same function
    ``create_custom_field`` and ``create_sequence`` call — rather than a second
    opinion about what a name may be, and with the limit read off the model's own
    column so the two cannot disagree. Without this, a template naming a
    120-character tag reaches the service inside ``apply_import``'s transaction
    and raises there, which is a 500 for a document a stranger supplied.

    A name that is already taken is reported as such rather than attempted:
    ``create_custom_field`` and ``create_sequence`` refuse a duplicate, and
    "use the existing one" is what the person meant anyway.
    """
    from apps.common.naming import clean_name

    try:
        resolution.name = clean_name(
            str(answer.get("name") or requirement.name),
            limit=_name_limit(requirement.kind),
            noun=noun,
            error=ValueError,
        )
    except ValueError as exc:
        resolution.problem = str(exc)
        return resolution

    # A tag is get-or-create (``get_or_create_tag``), so an existing one is not a
    # collision. Everything else refuses a duplicate.
    if requirement.kind != refs.KIND_TAG:
        taken = _find_by_name(workspace, Requirement(kind=requirement.kind, key="", name=resolution.name))
        if taken is not None:
            resolution.problem = f"A {noun} called “{resolution.name}” already exists — use the existing one instead."
            return resolution

    if requirement.kind == refs.KIND_CUSTOM_FIELD:
        resolution.literal = str(answer.get("field_type") or requirement.detail or "text")
        if resolution.literal not in _field_type_values():
            resolution.problem = "That is not a field type."
    return resolution


def _name_limit(kind: str) -> int:
    """The name column's width for a creatable kind, read off the model itself."""
    if kind == refs.KIND_FLOW:
        return Flow._meta.get_field("name").max_length or 200
    spec = _MODELS.get(kind)
    model = installed_model(*spec) if spec else None
    if model is None:  # pragma: no cover - the kind would not be creatable
        return 100
    return model._meta.get_field("name").max_length or 100


def _field_type_values() -> tuple[str, ...]:
    from apps.contacts.models import CustomFieldType

    return tuple(CustomFieldType.values)


def _existing(workspace: Any, requirement: Requirement, raw_id: Any) -> tuple[Any, str] | None:
    """``(id, name)`` for the object an answer names, scoped to the workspace."""
    parsed = _uuid(raw_id)
    if parsed is None:
        return None

    if requirement.kind == refs.KIND_MEMBER:
        from apps.members.models import WorkspaceMembership

        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=parsed).select_related("user").first()
        )
        return (membership.user_id, "") if membership is not None else None

    if requirement.kind == refs.KIND_FLOW:
        row = Flow.objects.for_workspace(workspace).filter(pk=parsed).first()
        return (row.pk, row.name) if row is not None else None

    if requirement.kind == "platform":
        model = installed_model("channels", "apps.channels", "ChannelConnection")
        if model is None:
            return None
        row = model.objects.for_workspace(workspace).filter(pk=parsed, platform=requirement.key).first()
        return (row.pk, row.display_name) if row is not None else None

    spec = _MODELS.get(requirement.kind)
    if spec is None:
        return None
    model = installed_model(*spec)
    if model is None:
        return None
    label = "filename" if requirement.kind == refs.KIND_MEDIA else "name"
    row = model.objects.for_workspace(workspace).filter(pk=parsed).values("id", label).first()
    return (row["id"], str(row[label])) if row is not None else None


def _find_by_name(workspace: Any, requirement: Requirement) -> Any | None:
    """The id of an object of this name already in the workspace, if there is one."""
    if not requirement.name or requirement.kind in (refs.KIND_MEMBER, refs.KIND_MEDIA, "platform"):
        return None
    if requirement.kind == refs.KIND_FLOW:
        row = Flow.objects.for_workspace(workspace).filter(name__iexact=requirement.name).first()
        return row.pk if row is not None else None
    spec = _MODELS.get(requirement.kind)
    if spec is None:
        return None
    model = installed_model(*spec)
    if model is None:
        return None
    row = model.objects.for_workspace(workspace).filter(name__iexact=requirement.name).values("id").first()
    return row["id"] if row is not None else None


def _uuid(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


# --------------------------------------------------------------------------
# Step 5: the only writes in this module
# --------------------------------------------------------------------------


@transaction.atomic
def apply_import(workspace: Any, document: dict[str, Any], mapping: dict[str, Any], *, user: Any = None) -> list[Flow]:
    """Create the flows. One transaction, and nothing partial.

    Refuses outright if any requirement is still unanswered, re-checking rather
    than trusting the dry run: the plan was computed against a workspace that
    may have changed since, and "the tag you mapped to has been deleted" must
    not be discovered halfway through writing a graph.
    """
    plan = plan_import(workspace, document, mapping)
    if not plan.can_apply:
        raise ImportNotReadyError(plan)

    for resolution in plan.resolutions:
        if resolution.action == ACTION_CREATE:
            resolution.target_id = str(_create(workspace, resolution))

    kept = {choice.key for choice in plan.triggers if choice.keep}

    flows: dict[str, Flow] = {}
    for entry in document["flows"]:
        flows[entry["key"]] = _create_flow(workspace, entry, user=user)

    # Indexed by the requirement's key *and* by the synthetic reference the
    # graphs actually carry. An id-addressed site holds the reference and knows
    # nothing about the name its question was folded onto, so without the second
    # index it would find no answer and leave the reference in place.
    lookup: dict[Any, Resolution] = {}
    for resolution in plan.resolutions:
        lookup[(resolution.requirement.kind, resolution.requirement.key)] = resolution
        if resolution.requirement.ref:
            lookup[(resolution.requirement.kind, resolution.requirement.ref)] = resolution
    for entry in document["flows"]:
        flow = flows[entry["key"]]
        graph = refs.rewrite_graph(entry["graph"], _substitute(lookup, flows, entry["key"]))
        _save(flow, graph, user=user)
        _create_triggers(flow, entry, lookup, flows, kept)

    logger.info(
        "Imported %s flow(s) into workspace %s as drafts, triggers disabled.",
        len(flows),
        getattr(workspace, "pk", workspace),
    )
    return [flows[entry["key"]] for entry in document["flows"]]


class ImportNotReadyError(RuntimeError):
    """Confirmation was attempted with questions still unanswered."""

    def __init__(self, plan: ImportPlan) -> None:
        super().__init__("Some requirements have not been answered.")
        self.plan = plan


def _create(workspace: Any, resolution: Resolution) -> Any:
    """Make the one object a ``create`` answer asked for, through its own service."""
    kind = resolution.requirement.kind
    if kind == refs.KIND_TAG:
        from apps.contacts.services import get_or_create_tag

        tag, _ = get_or_create_tag(workspace, resolution.name)
        return tag.pk
    if kind == refs.KIND_CUSTOM_FIELD:
        from apps.contacts.services import create_custom_field

        return create_custom_field(workspace, name=resolution.name, field_type=resolution.literal or "text").pk
    if kind == refs.KIND_SEQUENCE:
        from apps.campaigns.services import create_sequence

        return create_sequence(workspace, name=resolution.name).pk
    if kind == refs.KIND_FLOW:
        # An empty draft standing in for a flow the file did not carry. Through
        # ``create_flow`` like every other flow, so it has its version 1 and
        # behaves like one — it simply has nothing on the canvas yet.
        from apps.flows.services import create_flow

        return create_flow(workspace=workspace, name=resolution.name[:200]).pk
    raise ValueError(f"{kind!r} cannot be created from an import.")  # pragma: no cover - guarded by CREATABLE_KINDS


def _create_flow(workspace: Any, entry: dict[str, Any], *, user: Any) -> Flow:
    from apps.flows.services import create_flow

    return create_flow(
        workspace=workspace,
        name=str(entry["name"])[:200],
        folder=str(entry.get("folder") or "")[:200],
        user=user,
    )


def _save(flow: Flow, graph: Any, *, user: Any) -> None:
    """Write the graph through the ordinary draft path.

    ``save_draft`` rather than a fresh ``FlowVersion``: it sanitizes declared
    HTML fields on the way in, and ``create_flow`` has already made version 1 as
    an empty unpublished draft, so this updates that row rather than opening a
    second version.
    """
    from apps.flows.services import save_draft

    save_draft(flow, graph, user=user)


def _create_triggers(
    flow: Flow,
    entry: dict[str, Any],
    lookup: dict[Any, Resolution],
    flows: dict[str, Flow],
    kept: set[str],
) -> None:
    """Triggers, rewritten and **disabled** — and only the ones being kept."""
    from apps.flows.triggers.services import TriggerValidationError, create_trigger

    for index, trigger in enumerate(entry["triggers"]):
        if f"{entry['key']}:trigger-{index}" not in kept:
            continue
        config = refs.rewrite_trigger_config(
            trigger["type"], trigger["config"], _substitute(lookup, flows, entry["key"], f"trigger-{index}")
        )
        platform = trigger.get("platform")
        connection = None
        if isinstance(platform, str) and platform:
            resolution = lookup.get(("platform", platform))
            if resolution is not None and resolution.target_id:
                connection = _connection(flow.workspace_id, resolution.target_id)
        try:
            create_trigger(flow, trigger_type=trigger["type"], config=config, connection=connection, enabled=False)
        except TriggerValidationError as exc:
            # Every config here already passed validate_config on the way in, so
            # reaching this means the *rewrite* produced something the type will
            # not take — a mapped connection on a type that cannot bind, say.
            # The flow is worth more than the trigger: keep the flow, drop the
            # trigger, and say so.
            logger.warning(
                "Import: trigger %s of flow %s was refused and not created: %s",
                trigger["type"],
                flow.pk,
                "; ".join(issue.message for issue in exc.issues),
            )


def _connection(workspace_id: Any, connection_id: str) -> Any:
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:
        return None
    return model.objects.for_workspace(workspace_id).filter(pk=connection_id).first()


def _substitute(lookup: dict[Any, Resolution], flows: dict[str, Flow], flow_key: str, location: str | None = None):
    """A ``visit`` that writes the target workspace's own ids into the document."""

    def visit(site: refs.Site) -> Any:
        where = f"{flow_key}:{location if location is not None else (site.node_id or 'graph')}"

        if site.kind == refs.KIND_REQUEST_HEADER:
            resolution = lookup.get((site.kind, f"{where}:{site.detail or ''}"))
            return resolution.literal if resolution is not None else ""
        if site.kind in (refs.KIND_LINK_HANDLE, refs.KIND_FROM_OVERRIDE):
            resolution = lookup.get((site.kind, where))
            return resolution.literal if resolution is not None else site.value
        if site.kind == refs.KIND_COMMENT_POSTS:
            resolution = lookup.get((site.kind, where))
            # An empty answer stays an empty **list**, not a removed key. The
            # matcher treats "specific posts, none listed" as matching nothing
            # (``apps.flows.triggers.matching``), so it fails closed either way
            # — and keeping the key is what lets the trigger round-trip.
            return _post_ids(resolution.literal if resolution is not None else "")
        if site.kind == refs.KIND_WHATSAPP_TEMPLATE:
            # The id is optional and the flow runs without it, so the worst case
            # is a builder that cannot re-open the picker on the right row.
            # Re-resolving it from the reference costs one scoped lookup and
            # turns that worst case into the ordinary one.
            local = _whatsapp_template_id(flows, site.detail)
            return local if local else refs.REMOVE

        if site.addressing == refs.ADDRESS_NAME:
            name = str(site.value).strip() if isinstance(site.value, str) else ""
            resolution = lookup.get((site.kind, name.casefold())) if name else None
            return resolution.name or site.value if resolution is not None else site.value

        raw = str(site.value) if isinstance(site.value, str) else ""
        resolution = lookup.get((site.kind, raw)) if raw else None
        if resolution is None:
            return site.value
        if resolution.requirement.in_document:
            target = flows.get(resolution.requirement.in_document)
            return str(target.pk) if target is not None else site.value
        if resolution.literal and site.kind == refs.KIND_MEDIA:
            # A media requirement answered with a URL rather than a library
            # asset — which is how a template with a picture imports into a
            # workspace whose library is empty. SPEC §11.1 takes either, but
            # they are different keys, so the block rewriter does the swap.
            return refs.AsUrl(resolution.literal)
        return resolution.target_id or site.value

    return visit


def _whatsapp_template_id(flows: dict[str, Flow], reference: str) -> str:
    """This workspace's approved template with that ``<name>/<language>``, if any.

    Scoped through ``for_workspace`` off a flow that has just been created here,
    so the reference in an imported document can only ever resolve inside the
    importing workspace (SECURITY-BASELINE §1).
    """
    model = installed_model("channels", "apps.channels", "WhatsAppTemplate")
    if model is None or not reference or "/" not in reference or not flows:
        return ""
    name, _, language = reference.partition("/")
    workspace_id = next(iter(flows.values())).workspace_id
    row = model.objects.for_workspace(workspace_id).filter(name=name, language=language).values("id").first()
    return str(row["id"]) if row else ""


def _post_ids(raw: str) -> list[str]:
    """A comma- or newline-separated list of platform post ids, bounded."""
    from apps.flows.triggers.schema import MAX_POST_IDS

    parts = [part.strip() for chunk in raw.splitlines() for part in chunk.split(",")]
    return [part[:200] for part in parts if part][:MAX_POST_IDS]
