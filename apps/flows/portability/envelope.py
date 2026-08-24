"""The export envelope: its version, its caps, and the schema that guards it.

An import is **untrusted input**. A shared template arrives from a stranger, so
every check in this module happens before anything reaches the ORM, and in the
order :mod:`apps.flows.schema.envelope` established for the graph it wraps:
size, then parse, then shape, then the documents inside. Measuring or walking a
payload you have not bounded is how a hostile file becomes the denial of service
the cap was added to prevent.

--------------------------------------------------------------------------
Two versions, and why both are on the wire
--------------------------------------------------------------------------

``format`` is this envelope's own version. ``schema`` is
:data:`apps.flows.schema.envelope.SCHEMA_VERSION`, the graph format the flows
inside were written against — the same integer each ``graph`` already carries in
its own ``schema`` key, repeated at the top so a reader (a human, a future
migration, a validator) can tell what it is holding without descending into it.
Neither is guessed at when it does not match: a future format wants a migration,
and a migration that never ran is worse than a refusal.

--------------------------------------------------------------------------
`flows` is always a list
--------------------------------------------------------------------------

A single-flow export is a list of one. Bundle export — following ``start_flow``
and sequence-step references to their closure — then needs no second envelope
shape, so there is one schema, one validator and one importer instead of two
that drift. ``entry`` names the flow that was actually asked for, which is what
the import summary shows first.

--------------------------------------------------------------------------
`requirements` is advisory
--------------------------------------------------------------------------

The manifest tells a human what a template needs. The **importer never trusts
it**: :mod:`apps.flows.portability.imports` re-derives the requirement set by
walking the graphs and trigger configs, so a document whose manifest omits a
reference cannot talk the importer into leaving one dangling. What the manifest
does supply is *labels* — the name behind a synthetic reference — which are
displayed and used as defaults, never as authority.
"""

import json
from typing import Any

from apps.flows.portability.refs import (
    KIND_COMMENT_POSTS,
    KIND_CUSTOM_FIELD,
    KIND_FLOW,
    KIND_FROM_OVERRIDE,
    KIND_LINK_HANDLE,
    KIND_MEDIA,
    KIND_MEMBER,
    KIND_REQUEST_HEADER,
    KIND_SEGMENT,
    KIND_SEQUENCE,
    KIND_TAG,
    KIND_WHATSAPP_TEMPLATE,
)
from apps.flows.schema import fields as f
from apps.flows.schema.envelope import ID_PATTERN, SCHEMA_VERSION, json_depth
from apps.flows.schema.issues import Issue
from apps.flows.schema.jsonschema import CODE_INVALID_VALUE, validate_instance

__all__ = [
    "APP_NAME",
    "FORMAT_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_DEPTH",
    "MAX_FLOWS",
    "MAX_REQUIREMENTS_PER_KIND",
    "MAX_TRIGGERS_PER_FLOW",
    "REQUIREMENT_KINDS",
    "document_schema",
    "parse",
    "serialize",
    "validate_envelope",
]

#: The one export format v1 speaks.
FORMAT_VERSION = 1

#: Stamped so a document from another product cannot be half-read as one of
#: ours before something notices.
APP_NAME = "brightbean-chat"

#: Serialized ceiling for a whole document. Each graph inside is separately
#: capped at ``MAX_GRAPH_BYTES`` (512 KiB) by the graph validator, so this is
#: the *bundle's* bound: comfortably above twenty realistic flows and still
#: small enough that parsing one is cheap.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024

#: Nesting ceiling for the envelope. The deepest legitimate path is
#: document → flows → flow → graph → nodes → node → config → blocks → block →
#: cards → card → url_button, so this is ``MAX_GRAPH_DEPTH`` plus the three
#: levels this envelope adds on top of a bare graph.
MAX_DOCUMENT_DEPTH = 24

#: How many flows one document may carry. A bundle is a closure, not an export
#: of a workspace; twenty is far past any real template and bounds the work the
#: import wizard has to render.
MAX_FLOWS = 20

#: SPEC §10 gives a flow as many triggers as it likes, but a shared template
#: carrying hundreds is a document to refuse rather than a page to render.
MAX_TRIGGERS_PER_FLOW = 50

#: Per requirement kind. The manifest is derived from the graphs, so this only
#: ever binds a hand-written document.
MAX_REQUIREMENTS_PER_KIND = 200

#: How many "used by" addresses one requirement lists.
MAX_USED_BY = 200

#: The manifest's sections, in a fixed order. Every key is always present, even
#: when empty: a client that has to branch on which keys exist is one that
#: breaks on the day a template needs the section it never saw.
REQUIREMENT_KINDS: tuple[str, ...] = (
    KIND_TAG,
    KIND_CUSTOM_FIELD,
    KIND_SEQUENCE,
    KIND_SEGMENT,
    KIND_MEMBER,
    KIND_FLOW,
    KIND_MEDIA,
    "platform",
    KIND_REQUEST_HEADER,
    KIND_WHATSAPP_TEMPLATE,
    KIND_LINK_HANDLE,
    KIND_FROM_OVERRIDE,
    KIND_COMMENT_POSTS,
)


def _issue(message: str, path: str, code: str = CODE_INVALID_VALUE) -> Issue:
    return Issue(code=code, message=message, stage="document", path=path)


# --------------------------------------------------------------------------
# The schema
# --------------------------------------------------------------------------

#: One manifest entry. Deliberately one shape for every kind rather than
#: thirteen: ``obj`` closes the object either way (SECURITY-BASELINE §7's
#: mass-assignment guard), and the kind-specific part of an entry is a label
#: shown to a human, not a field anything branches on.
_REQUIREMENT = f.obj(
    {
        "key": f.string(min_length=1, max_length=300, description="Identifies this requirement within its kind."),
        "ref": f.string(
            max_length=64,
            description="The synthetic id standing in for this object inside the graphs. Absent when nothing "
            "addresses it by id.",
        ),
        "name": f.string(max_length=300, description="What the exporting workspace called it. A label and a default."),
        "detail": f.string(max_length=300, description="Kind-specific context: a media kind, a header name."),
        "used_by": f.array(
            f.string(max_length=200),
            max_items=MAX_USED_BY,
            description="Where it is referenced, as <flow key>:<node or trigger>. Carries no workspace data.",
        ),
    },
    required=["key"],
)

#: One trigger. ``platform`` rather than a connection id: SPEC §5 makes a null
#: ``channel_connection`` mean "all connections of a matching platform", so null
#: here is a *value* and not an omission, and a bound trigger exports the
#: platform its connection was on so the import can offer the right ones.
_TRIGGER = f.obj(
    {
        "type": f.string(min_length=1, max_length=20),
        "platform": {
            **f.string(max_length=20),
            "type": ["string", "null"],
            "description": "The platform the source trigger was bound to, or null for every matching connection.",
        },
        "config": f.any_json(description="Validated against the trigger type's own schema, not against this one."),
    },
    required=["type", "config"],
)

_FLOW = f.obj(
    {
        "key": f.string(
            min_length=1,
            max_length=64,
            pattern=ID_PATTERN,
            description="Names this flow inside the document; how start_flow references point at it.",
        ),
        "name": f.string(min_length=1, max_length=200),
        "folder": f.string(max_length=200),
        "graph": f.any_json(description="Validated against the flow-graph schema, not against this one."),
        "triggers": f.array(_TRIGGER, max_items=MAX_TRIGGERS_PER_FLOW),
    },
    required=["key", "name", "graph", "triggers"],
)


def document_schema() -> dict[str, Any]:
    """The envelope's JSON Schema.

    ``graph`` and ``config`` are ``any_json`` here on purpose. They are whole
    documents with their own validators — ``apps.flows.schema.validate_graph``
    and ``apps.flows.triggers.validation.validate_config`` — and describing them
    a second time in this module is exactly the duplicated rule table that
    ROADMAP contract 2 exists to prevent. :func:`validate_envelope` hands each
    one to its owner.
    """
    return f.obj(
        {
            "app": f.const(APP_NAME),
            "format": {"type": "integer", "const": FORMAT_VERSION},
            "schema": {"type": "integer", "const": SCHEMA_VERSION},
            "entry": f.string(min_length=1, max_length=64, pattern=ID_PATTERN),
            "flows": f.array(_FLOW, min_items=1, max_items=MAX_FLOWS),
            "requirements": f.obj(
                {kind: f.array(_REQUIREMENT, max_items=MAX_REQUIREMENTS_PER_KIND) for kind in REQUIREMENT_KINDS}
            ),
        },
        required=["app", "format", "schema", "entry", "flows", "requirements"],
    )


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def serialize(document: dict[str, Any]) -> str:
    """The document's exact bytes, as text.

    Canonical on purpose, and the same convention as
    ``static/flows/flow-schema.json``: sorted keys, two-space indent, no
    timestamp, no version stamp beyond the two the format carries. That is what
    makes the round-trip guarantee an assertion about **bytes** rather than a
    bespoke "semantically identical" comparison nobody can audit — export,
    import into a clean workspace, export again, and diff.
    """
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def parse(raw: bytes | str) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Bytes to a document, or the findings that stop us. Never raises.

    Size first, because the parse is the expense. Depth second, iteratively,
    because a recursive measurement of a hostile document is itself the denial
    of service it was added to prevent.
    """
    size = len(raw.encode("utf-8") if isinstance(raw, str) else raw)
    if size > MAX_DOCUMENT_BYTES:
        return None, [_issue(f"The file is {size} bytes; the limit is {MAX_DOCUMENT_BYTES} bytes.", "document")]
    if not size:
        return None, [_issue("The file is empty.", "document")]

    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError is belt and braces: CPython's parser handles deeper
        # nesting than the depth cap allows, so in practice the cap catches
        # those first — but a parser that gives up must not be a 500.
        return None, [_issue("The file is not valid JSON.", "document")]

    if not isinstance(payload, dict):
        return None, [_issue("A flow template must be a JSON object.", "document")]

    if json_depth(payload, limit=MAX_DOCUMENT_DEPTH) > MAX_DOCUMENT_DEPTH:
        return None, [_issue(f"The file nests deeper than the limit of {MAX_DOCUMENT_DEPTH} levels.", "document")]
    return payload, []


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_envelope(document: Any) -> list[Issue]:
    """Envelope shape, versions, then every graph and every trigger config.

    Findings here refuse the import outright — there is no half-imported flow.
    The order matters: nothing below the shape check can be trusted to be the
    shape it looks like, and a cascade of consequential complaints would bury
    the one that describes the actual problem.
    """
    if not isinstance(document, dict):
        return [_issue("A flow template must be a JSON object.", "document")]

    issues = validate_instance(document_schema(), document, path="")
    if issues:
        return issues

    for index, flow in enumerate(document["flows"]):
        issues.extend(_validate_flow(flow, f"flows[{index}]"))

    keys = [flow["key"] for flow in document["flows"]]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    for key in duplicates:
        issues.append(_issue(f"Two flows share the key {key!r}.", "flows"))

    if document["entry"] not in keys:
        issues.append(_issue(f"entry names {document['entry']!r}, which is not a flow in this file.", "entry"))

    return issues


def _validate_flow(flow: dict[str, Any], path: str) -> list[Issue]:
    """One flow's graph and triggers, through their existing validators."""
    from apps.flows.schema import validate_graph
    from apps.flows.triggers.validation import validate_config

    issues: list[Issue] = []

    # validate_graph runs validate_document first, which is where
    # MAX_GRAPH_BYTES, MAX_GRAPH_DEPTH, MAX_NODES and MAX_EDGES are enforced.
    # `platforms=()` because an imported flow has no channels yet: capability
    # warnings are computed for real once it lands in a workspace.
    result = validate_graph(flow["graph"])
    for finding in result.document_errors:
        issues.append(
            Issue(
                code=finding.code,
                message=f"{path}: {finding.message}",
                stage="document",
                node_id=finding.node_id,
                edge_id=finding.edge_id,
                path=finding.path,
            )
        )

    # Graph errors — a dangling edge, no entry node — do **not** stop an import.
    # A half-wired flow is an ordinary draft (SPEC §16's autosave saves them),
    # and refusing to import one would be stricter than the builder that made
    # it. They surface in the dry run and they block publish, as they always do.

    for index, trigger in enumerate(flow["triggers"]):
        for finding in validate_config(trigger["type"], trigger["config"]):
            issues.append(
                Issue(
                    code=finding.code,
                    message=f"{path}.triggers[{index}]: {finding.message}",
                    stage="document",
                    path=finding.path,
                )
            )

    return issues
