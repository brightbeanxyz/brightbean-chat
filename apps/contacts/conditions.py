"""The condition engine (ROADMAP contract 8, SPEC §11.4).

One filter language, four consumers: saved segments, the flow Condition node
(issue #9), broadcast targeting (#23) and inbox rules (#24). They all call the
same three names, and nothing re-implements the operator table:

    CONDITION_SCHEMA                          # JSON Schema, embedded by issue #6
    validate(workspace, filter_json)          # -> CompiledFilter, or raises
    evaluate(contact, filter_json) -> bool    # one contact
    queryset(workspace, filter_json)          # -> QuerySet[Contact]

``evaluate()`` is ``queryset(...).filter(pk=...).exists()``. That is the whole
implementation, and it is deliberate. The alternative — a second, in-Python
evaluator — would mean writing the absence semantics, the day boundaries, the
complement rule and the match algebra twice, and the failure mode of a
disagreement is a contact who receives a message the segment says they will not
receive: invisible in review, unattributable in production.

--------------------------------------------------------------------------
Vocabulary is frozen here; the registry supplies behaviour only
--------------------------------------------------------------------------

``CONDITION_SCHEMA`` is generated from the tables in this module, **not** from
:data:`_SOURCES`. If it were built from the registry its content would depend on
which apps had imported, so the schema issue #6 embeds would differ between a
dev box and CI. So all six sources — including ``window`` (issue #8) and
``sequence`` (#22), which nothing implements yet — are declared here with their
operators. A filter using an unimplemented source **validates and can be saved**;
evaluating one raises :class:`SourceNotEvaluableError` naming the owning issue.

--------------------------------------------------------------------------
Why no user string ever reaches a query kwarg
--------------------------------------------------------------------------

SECURITY-BASELINE §7 requires ORM-only compilation behind a field/operator
allowlist. That is structural here, not a promise:

* ``op`` is only ever a **key into a frozen dict** (``_TEXT_PREDICATE[op]``,
  ``_DATE_PREDICATE[type, op]``). It is never concatenated into a lookup.
* ``key`` is either a UUID, which reaches the ORM only as a bound parameter
  after being confirmed to exist *in this workspace*, or a name in
  :data:`SYSTEM_FIELDS` — and it is the mapped ``column``, a module constant,
  that becomes part of a kwarg.
* :func:`_lookup` is the only function in the module that builds a lookup
  string, and both halves come from module constants.

So ``{"key": "email__regex"}`` dies at the ``SYSTEM_FIELDS`` membership check and
``{"op": "regex"}`` dies at the vocabulary check — both before any database
access at all, which the test suite asserts by capturing queries.

--------------------------------------------------------------------------
``Exists()`` and the workspace-scoping guard
--------------------------------------------------------------------------

``WorkspaceScopedQuerySet`` refuses to *execute* unscoped. A queryset handed to
``Exists()`` is never executed — it is compiled into the outer statement — so
``_assert_scoped`` never runs and the guard gives this module **no protection at
all**. The same is true of ``Subquery()``, ``__in=<queryset>`` and
``annotate()``.

Therefore every subquery below is built with ``.for_workspace(...)``, and the
``workspace_id = %s`` predicate it adds inside each ``EXISTS`` is load-bearing
isolation rather than belt-and-braces. Anyone deleting it as "redundant with the
contact join" is removing the only tenancy check the subquery has. Two tests pin
this: one reads the compiled SQL, and one asserts an unscoped subquery really
does compile without raising, so the hazard is recorded as an executable fact.

--------------------------------------------------------------------------
Semantics worth knowing before reading a filter
--------------------------------------------------------------------------

* **Negatives include absence.** ``is_not``, ``no_value``, ``has_not`` and
  ``not_in`` compile to ``~<positive>``, so a contact with no tag row and no
  field row matches them. "Everyone not tagged VIP" must include the contacts
  who have never been tagged with anything. This also makes each pair an exact
  partition of the workspace, which the suite asserts numerically.
* **Every predicate is two-valued.** SQL ``NULL`` would make both halves of a
  pair false for the same row and quietly shrink the population, so predicates on
  a nullable column are wrapped by :func:`_definite`.
* **Dates are day-granular and resolve in the workspace's timezone**
  (``Workspace.effective_timezone``). SPEC §11.5 gives ``smart_delay`` an
  explicit ``use_contact_timezone`` flag and §11.4 has none — the absence is
  load-bearing. Per-contact boundaries would also force
  ``col AT TIME ZONE contact.timezone`` into the WHERE clause, which no index
  can serve.
* **Deleted contacts are never in the set.** ``status`` is a soft-delete marker,
  not a segmentation dimension, so it is absent from :data:`SYSTEM_FIELDS` and
  :func:`queryset` always restricts to active contacts. A filter that could
  target soft-deleted people would put them back in a send path.
* **An empty ``rules`` list under ``match: all`` matches everyone** (the identity
  of AND) and under ``match: any`` matches nobody. That is correct algebra and a
  live hazard: an empty segment handed to a broadcast targets the workspace.
  Issue #23 must show a count before sending.
"""

import json
import operator
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from decimal import Decimal, InvalidOperation
from functools import reduce
from itertools import chain
from types import MappingProxyType
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from apps.common.platforms import Platform
from apps.contacts.models import (
    VALUE_COLUMNS,
    Contact,
    ContactStatus,
    ContactTag,
    CustomField,
    CustomFieldType,
    CustomFieldValue,
    Segment,
    Tag,
)

__all__ = [
    "CONDITION_SCHEMA",
    "CompiledFilter",
    "ConditionError",
    "ConditionSource",
    "ConditionValidationError",
    "OPS_BY_SOURCE",
    "OPS_BY_TYPE",
    "SOURCE_NAMES",
    "SYSTEM_FIELDS",
    "SourceContractError",
    "SourceNotEvaluableError",
    "evaluate",
    "evaluate_many",
    "queryset",
    "register_source",
    "sources",
    "validate",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConditionError(Exception):
    """Base for everything this module raises."""


class ConditionValidationError(ConditionError, ValueError):
    """The filter document is not acceptable.

    A ``ValueError`` subclass so ``Segment.clean()`` can map it onto Django's
    ``ValidationError`` and so issue #6's builder API and #25's public API answer
    400 rather than 500 without importing a bespoke type.

    ``path`` points at the offending rule (``rules[2].op``) and ``code`` is a
    machine-readable slug the builder can map to a field-level message. Messages
    name *keys*, never echo values, following ``apps/credentials/forms.py``.
    """

    def __init__(self, message: str, *, path: str = "", code: str = "invalid") -> None:
        self.path = path
        self.code = code
        super().__init__(f"{path}: {message}" if path else message)


class SourceNotEvaluableError(ConditionError, RuntimeError):
    """A declared source with no implementation in this deployment.

    Deliberately not a ``ValueError``: the document is fine, the deployment
    cannot run it. Issue #9's failure policy should treat it as a node failure,
    not as a validation error.
    """


class SourceContractError(ConditionError, RuntimeError):
    """A registration tried to change the frozen vocabulary."""


# ---------------------------------------------------------------------------
# Vocabulary — frozen at L2-A, straight from SPEC §11.4
# ---------------------------------------------------------------------------

MATCH_ALL = "all"
MATCH_ANY = "any"
MATCH_MODES: tuple[str, ...] = (MATCH_ALL, MATCH_ANY)

TYPE_TEXT = CustomFieldType.TEXT.value
TYPE_NUMBER = CustomFieldType.NUMBER.value
TYPE_DATE = CustomFieldType.DATE.value
TYPE_DATETIME = CustomFieldType.DATETIME.value
TYPE_BOOLEAN = CustomFieldType.BOOLEAN.value

#: Operators by value type, transcribed from SPEC §11.4 exactly — including the
#: symbolic number operators, because issue #6 is coding against the spec text
#: right now and swaps to this import when L2-A merges. ``has_value``/``no_value``
#: are text-only for the same reason; widening them to every type is an additive
#: schema change a later issue can make, but a divergence today would break the
#: version #6 has already vendored.
OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    TYPE_TEXT: ("is", "is_not", "contains", "has_value", "no_value"),
    TYPE_NUMBER: ("=", "!=", ">", "<", ">=", "<="),
    TYPE_DATE: ("before", "after", "on"),
    TYPE_DATETIME: ("before", "after", "on"),
    TYPE_BOOLEAN: ("is",),
}

#: Operators for sources whose ops are native rather than type-derived. SPEC
#: §11.4 writes the sequence pair as "subscribed/not", so ``not`` is the literal
#: token; :data:`OP_LABELS` gives it something a human can read.
OPS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "tag": ("has", "has_not"),
    # SPEC §11.4 lists `segment` as a source but gives it no operators. A segment
    # is a saved predicate rather than a value, so membership is the only
    # question there is to ask; `in`/`not_in` mirrors the shape of every other
    # source-native pair and gives the negative half the complement test needs.
    "segment": ("in", "not_in"),
    "sequence": ("subscribed", "not"),
    "window": ("inside", "outside"),
}

SOURCE_NAMES: tuple[str, ...] = ("tag", "custom_field", "system_field", "segment", "sequence", "window")

SOURCE_LABELS: dict[str, str] = {
    "tag": "Tag",
    "custom_field": "Custom field",
    "system_field": "Contact field",
    "segment": "Segment",
    "sequence": "Sequence",
    "window": "Messaging window",
}

#: Operators that take no ``value`` at all — the rule's ``key`` carries the whole
#: subject. A rule that supplies one anyway is **rejected**, not quietly
#: stripped: a saved filter must never carry a value the author believes is doing
#: something.
VALUELESS_OPS: frozenset[str] = frozenset(
    {"has_value", "no_value", "has", "has_not", "in", "not_in", "subscribed", "not", "inside", "outside"}
)

#: (positive, negative). The compiler derives every negative as ``~positive``,
#: and the suite asserts |pos| + |neg| == |population| off this same tuple, so a
#: new pair cannot ship with only one half proven. ``contains`` is absent because
#: SPEC §11.4 gives it no negative partner.
NEGATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("is", "is_not"),
    ("=", "!="),
    ("has_value", "no_value"),
    ("has", "has_not"),
    ("in", "not_in"),
    ("subscribed", "not"),
    ("inside", "outside"),
)

_POSITIVE_OF: dict[str, str] = {negative: positive for positive, negative in NEGATION_PAIRS}

OP_LABELS: dict[str, str] = {
    "is": "is",
    "is_not": "is not",
    "contains": "contains",
    "has_value": "is set",
    "no_value": "is not set",
    "=": "=",
    "!=": "≠",
    ">": ">",
    "<": "<",
    ">=": "≥",
    "<=": "≤",
    "before": "before",
    "after": "after",
    "on": "on",
    "has": "has tag",
    "has_not": "does not have tag",
    "in": "in segment",
    "not_in": "not in segment",
    "subscribed": "subscribed to",
    "not": "not subscribed to",
    "inside": "inside the messaging window",
    "outside": "outside the messaging window",
}

TYPE_OPS: frozenset[str] = frozenset(chain.from_iterable(OPS_BY_TYPE.values()))
ALL_OPS: frozenset[str] = TYPE_OPS | frozenset(chain.from_iterable(OPS_BY_SOURCE.values()))


@dataclass(frozen=True)
class SystemField:
    """One allowlisted column on ``Contact``.

    The key is decoupled from the column name on purpose: a filter lives inside
    a saved segment and inside a published flow graph, so a future column rename
    must not invalidate documents already on disk.
    """

    column: str
    type: str
    label: str
    nullable: bool = False


#: The system-field allowlist. ``status`` is deliberately absent (see the module
#: docstring); ``created_at`` is present although SPEC §5 does not list it under
#: contacts, because ``BaseModel`` gives every row one and "created in the last
#: 7 days" is the filter every onboarding flow wants.
SYSTEM_FIELDS: dict[str, SystemField] = {
    "first_name": SystemField("first_name", TYPE_TEXT, "First name"),
    "last_name": SystemField("last_name", TYPE_TEXT, "Last name"),
    "email": SystemField("email", TYPE_TEXT, "Email"),
    "phone": SystemField("phone", TYPE_TEXT, "Phone"),
    "locale": SystemField("locale", TYPE_TEXT, "Locale"),
    "timezone": SystemField("timezone", TYPE_TEXT, "Timezone"),
    "created_at": SystemField("created_at", TYPE_DATETIME, "Created"),
    "last_interaction_at": SystemField("last_interaction_at", TYPE_DATETIME, "Last interaction", nullable=True),
}

RELATIVE_UNITS: tuple[str, ...] = ("days",)

# Limits (SECURITY-BASELINE §7). A 50-rule filter serialises to roughly 4 KiB.
MAX_FILTER_BYTES = 16 * 1024
MAX_JSON_DEPTH = 6
MAX_RULES = 50
MAX_TOTAL_RULES = 200
MAX_SEGMENT_DEPTH = 3
MAX_KEY_CHARS = 200
MAX_VALUE_CHARS = 500
MAX_RELATIVE_OFFSET = 3650
MAX_ABS_NUMBER = 1e15


# ---------------------------------------------------------------------------
# Parsed forms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelativeDay:
    """A date expressed relative to "now" — SPEC §11.4's "days ago / days from now".

    Encoded as a signed offset rather than a direction word plus a magnitude:
    one integer needs one widget and one validation branch, and it has no state
    where both halves disagree.

    Left **unresolved** by ``validate()`` and resolved at compile time, so a
    filter compiled once and reused for an hour does not carry a stale midnight
    — and so both entry points see the same instant when ``evaluate()``
    delegates to ``queryset()``.
    """

    unit: str
    offset: int

    def resolve(self, now: datetime, tz: tzinfo) -> date:
        return timezone.localtime(now, tz).date() + timedelta(days=self.offset)


@dataclass(frozen=True)
class Rule:
    """One validated rule, with everything the compiler needs already resolved."""

    index: int
    source: str
    op: str
    key: str
    value: Any = None
    value_type: str = ""
    target_id: Any = None
    group: "Group | None" = None

    def resolve_day(self, now: datetime, tz: tzinfo) -> date:
        return self.value.resolve(now, tz) if isinstance(self.value, RelativeDay) else self.value


@dataclass(frozen=True)
class Group:
    match: str
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class CompiledFilter:
    """A validated filter. Accepted by :func:`queryset` and :func:`evaluate`.

    Callers in a loop — issue #9's condition node, #23's fanout — validate once
    and pass this, so the resolution lookups are paid once per node rather than
    once per contact.
    """

    root: Group


@dataclass
class _Budget:
    """Running totals across a segment expansion."""

    rules: int = 0

    def spend(self, count: int) -> None:
        self.rules += count
        if self.rules > MAX_TOTAL_RULES:
            raise ConditionValidationError(
                f"a filter may expand to at most {MAX_TOTAL_RULES} rules across every segment it references",
                code="budget_exceeded",
            )


# ---------------------------------------------------------------------------
# Source registry — behaviour only; the vocabulary above is frozen
# ---------------------------------------------------------------------------

KEY_UUID = "uuid"
KEY_SYSTEM_FIELD = "system_field"
KEY_PLATFORM = "platform"


@dataclass(frozen=True)
class ConditionSource:
    """One rule source. Declared here at L2-A; ``build_q`` may arrive later.

    A ``build_q`` of ``None`` is a **declared slot**: filters using the source
    validate and can be saved, and raise :class:`SourceNotEvaluableError` if
    evaluated. That is what lets issue #6 ship the whole builder panel before
    #8 and #22 exist.

    Implementers: build every subquery with ``.for_workspace(...)``. The
    scoping guard does not fire inside ``Exists()`` (see the module docstring),
    so that predicate is the subquery's only tenancy check.
    """

    name: str
    label: str
    key_kind: str
    ops: tuple[str, ...]
    build_q: "Callable[[_Ctx, Rule], Q] | None" = None
    owner: str = ""

    @property
    def is_evaluable(self) -> bool:
        return self.build_q is not None


_SOURCES: dict[str, ConditionSource] = {}


def register_source(source: ConditionSource, *, replace: bool = False) -> None:
    """Supply the implementation for a source declared in this module.

    Called from ``AppConfig.ready()`` — issue #8 registers ``window``, issue #22
    registers ``sequence`` — mirroring how ``apps/common/apps.py`` imports its
    checks for the side effect.

    Idempotent for an identical registration: ``ready()`` runs twice under some
    autoreload paths, and that must not be an error.
    """
    existing = _SOURCES.get(source.name)
    if existing is None:
        raise SourceContractError(
            f"{source.name!r} is not a declared condition source. The vocabulary is frozen in "
            f"apps.contacts.conditions so CONDITION_SCHEMA cannot depend on import order; a new "
            f"source means editing this module and the builder panel that renders it."
        )
    if existing == source:
        return
    if tuple(existing.ops) != tuple(source.ops) or existing.key_kind != source.key_kind:
        raise SourceContractError(
            f"register_source({source.name!r}) would change the operator vocabulary from {existing.ops} "
            f"to {source.ops}. Issue #6 embedded CONDITION_SCHEMA and the flow builder generates its "
            f"panels from it — later layers supply behaviour, never vocabulary (ROADMAP contract 8)."
        )
    if existing.is_evaluable and not replace:
        raise SourceContractError(f"{source.name!r} already has an implementation.")
    _SOURCES[source.name] = source


def sources() -> Mapping[str, ConditionSource]:
    """Read-only view of the registry. ``is_evaluable`` greys out a slot in a UI."""
    return MappingProxyType(_SOURCES)


def _legal_ops(source: str) -> frozenset[str]:
    """Operators structurally legal for a source, before the field type narrows them."""
    return frozenset(OPS_BY_SOURCE[source]) if source in OPS_BY_SOURCE else TYPE_OPS


# ---------------------------------------------------------------------------
# CONDITION_SCHEMA — generated from the tables above, never hand-written
# ---------------------------------------------------------------------------

_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_KEY_SCHEMAS: dict[str, dict[str, Any]] = {
    KEY_UUID: {"type": "string", "pattern": _UUID_PATTERN},
    KEY_SYSTEM_FIELD: {"type": "string", "enum": sorted(SYSTEM_FIELDS)},
    KEY_PLATFORM: {"type": "string", "enum": sorted(Platform.values)},
}

_RELATIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relative"],
    "properties": {
        "relative": {
            "type": "object",
            "additionalProperties": False,
            "required": ["unit", "offset"],
            "properties": {
                "unit": {"type": "string", "enum": list(RELATIVE_UNITS)},
                "offset": {"type": "integer", "minimum": -MAX_RELATIVE_OFFSET, "maximum": MAX_RELATIVE_OFFSET},
            },
        }
    },
}

#: ``value`` is a bounded union, never a free-form document. That is what keeps
#: the whole filter's depth fixed — a schema-free ``value`` would be an
#: unbounded nesting hole no byte cap closes (SECURITY-BASELINE §7).
_VALUE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string", "maxLength": MAX_VALUE_CHARS},
        {"type": "number", "minimum": -MAX_ABS_NUMBER, "maximum": MAX_ABS_NUMBER},
        {"type": "boolean"},
        _RELATIVE_SCHEMA,
    ]
}


def _rule_variant(source: str, ops: Iterable[str], *, takes_value: bool) -> dict[str, Any]:
    """One ``oneOf`` branch of the rule schema, discriminated on ``source``.

    A source whose operators are a mix of value-taking and valueless gets **two**
    branches rather than one with an optional ``value``. That is the difference
    between a schema that describes the language and one that merely permits it:
    with a single branch, ``{"source": "custom_field", "op": ">"}`` — a
    comparison with nothing to compare against — validates, so issue #6 publishes
    the flow and :func:`validate` only refuses it later, when the node runs.
    Splitting lets ``required`` carry the rule, which is the one keyword every
    consumer of this schema already implements.
    """
    declaration = _SOURCES[source]
    properties: dict[str, Any] = {
        "source": {"const": source},
        "key": _KEY_SCHEMAS[declaration.key_kind],
        "op": {"type": "string", "enum": sorted(ops)},
    }
    required = ["source", "key", "op"]
    if takes_value:
        properties["value"] = _VALUE_SCHEMA
        required.append("value")
    return {
        "type": "object",
        "title": declaration.label if not takes_value else f"{declaration.label} comparison",
        # The mass-assignment guard, restated where the builder can see it. It is
        # also what makes the valueless branch reject a stray `value`.
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _rule_variants() -> list[dict[str, Any]]:
    """Every branch, in source order, valueless before value-taking."""
    variants: list[dict[str, Any]] = []
    for source in SOURCE_NAMES:
        legal = _legal_ops(source)
        valueless = legal & VALUELESS_OPS
        with_value = legal - VALUELESS_OPS
        if valueless:
            variants.append(_rule_variant(source, valueless, takes_value=False))
        if with_value:
            variants.append(_rule_variant(source, with_value, takes_value=True))
    return variants


def _build_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://brightbean.chat/schemas/condition-filter.json",
        "title": "Condition filter",
        "description": (
            "SPEC §11.4. Dates are day-granular and resolve in the workspace's timezone, never the "
            "contact's — §11.5's use_contact_timezone flag is deliberately absent here."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": ["match", "rules"],
        "properties": {
            "match": {"type": "string", "enum": list(MATCH_MODES), "default": MATCH_ALL},
            "rules": {
                "type": "array",
                "minItems": 0,
                "maxItems": MAX_RULES,
                "items": {"oneOf": _rule_variants()},
            },
        },
        # Everything JSON Schema structurally cannot express. The `op` enum for
        # custom_field and system_field is the union of every type's operators,
        # because the deciding type lives in a database row — so the builder
        # narrows client-side from `opsByType` once it knows the field, and the
        # server narrows in validate(). This is exactly why the schema is a
        # shape contract and NOT the security boundary.
        "x-brightbean": {
            "opsByType": {value_type: list(ops) for value_type, ops in OPS_BY_TYPE.items()},
            "opsBySource": {source: list(ops) for source, ops in OPS_BY_SOURCE.items()},
            "valuelessOps": sorted(VALUELESS_OPS),
            "negationPairs": [list(pair) for pair in NEGATION_PAIRS],
            "opLabels": dict(OP_LABELS),
            "systemFields": {name: {"type": f.type, "label": f.label} for name, f in SYSTEM_FIELDS.items()},
            "relativeUnits": list(RELATIVE_UNITS),
            "unimplementedSources": [name for name in SOURCE_NAMES if not _SOURCES[name].is_evaluable],
            "limits": {
                "maxRules": MAX_RULES,
                "maxTotalRules": MAX_TOTAL_RULES,
                "maxValueChars": MAX_VALUE_CHARS,
                "maxRelativeOffset": MAX_RELATIVE_OFFSET,
                "maxSegmentDepth": MAX_SEGMENT_DEPTH,
            },
        },
    }


# ---------------------------------------------------------------------------
# Validation — steps 1 to 3 touch no database and no ORM at all
# ---------------------------------------------------------------------------

_ROOT_KEYS = frozenset({"match", "rules"})
_RULE_KEYS = frozenset({"source", "key", "op", "value"})
_RULE_REQUIRED = frozenset({"source", "key", "op"})
MAX_NODES = 5_000

#: Charged per non-string node when estimating a parsed document's serialized
#: size — a number, a bool, a null, or the braces of a container.
_NODE_OVERHEAD_BYTES = 8


def _reject_constant(name: str) -> Any:
    """``json.loads`` accepts bare NaN/Infinity by default; Postgres does not.

    Left alone, ``float("nan")`` reaches psycopg and raises a ``DataError`` — a
    500 from a value a stranger controls.
    """
    raise ConditionValidationError(f"{name} is not a valid filter value", code="bad_number")


def _max_bracket_depth(raw: str) -> int:
    """Nesting depth of a JSON document without parsing it.

    The byte cap alone does not close the depth hole: 16 KiB of ``[`` is 16 000
    levels, far past the recursion limit, and the decoder's ``RecursionError``
    inside a request is a 500 rather than a 400. String-aware, so a value that
    contains a bracket is not counted.
    """
    depth = deepest = 0
    in_string = escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _assert_shape(document: Any) -> None:
    """Depth, node and **size** caps on an already-parsed document.

    Iterative rather than recursive: a recursive walk over a depth bomb is the
    very ``RecursionError`` the caps exist to prevent. Django's ``JSONField``
    hands back a ``dict``, so this is the only guard that runs on the path from
    a stored ``Segment.filter_json`` — which is the usual path, and the reason
    the size cap lives here rather than only beside ``json.loads``.

    Size is accumulated during the walk instead of by serialising the document:
    ``json.dumps`` on something this function has not yet vetted is the work the
    cap exists to bound, and it raises on values the walk handles calmly.
    The estimate counts each string's own length plus a small per-node overhead
    for the punctuation that would surround it, which is close enough for a
    limit whose job is to have an order of magnitude.
    """
    stack: list[tuple[Any, int]] = [(document, 1)]
    nodes = 0
    size = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise ConditionValidationError("filter document is too large", code="too_large")
        if depth > MAX_JSON_DEPTH:
            raise ConditionValidationError("filter document is nested too deeply", code="too_deep")
        size += len(node) + 2 if isinstance(node, str) else _NODE_OVERHEAD_BYTES
        if size > MAX_FILTER_BYTES:
            raise ConditionValidationError("filter document is too large", code="too_large")
        if isinstance(node, dict):
            stack.extend((child, depth + 1) for child in node.values())
            size += sum(len(str(key)) + 4 for key in node)
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)


def _load(filter_json: Any) -> Any:
    if isinstance(filter_json, bytes | bytearray):
        try:
            filter_json = bytes(filter_json).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConditionValidationError("filter document is not valid UTF-8", code="bad_encoding") from exc
    if isinstance(filter_json, str):
        if len(filter_json.encode("utf-8", "surrogatepass")) > MAX_FILTER_BYTES:
            raise ConditionValidationError("filter document is too large", code="too_large")
        # Depth is checked BEFORE json.loads, not after.
        if _max_bracket_depth(filter_json) > MAX_JSON_DEPTH:
            raise ConditionValidationError("filter document is nested too deeply", code="too_deep")
        try:
            filter_json = json.loads(filter_json, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            # Broader than JSONDecodeError on purpose. json.loads also raises a
            # bare ValueError for an integer literal past CPython 3.11's
            # 4300-digit conversion limit — which fits inside MAX_FILTER_BYTES,
            # so the size cap does not catch it — and a RecursionError is
            # conceivable for a document the bracket prescan let through. Every
            # one of those is a bad document, and a caller that catches
            # ConditionValidationError should not get a 500 for one.
            #
            # JSONDecodeError is a ValueError subclass, so it lands here too;
            # ConditionValidationError is also one, which is why _reject_constant's
            # own error is re-raised unchanged rather than relabelled.
            if isinstance(exc, ConditionValidationError):
                raise
            raise ConditionValidationError("filter document is not valid JSON", code="bad_json") from exc
    _assert_shape(filter_json)
    return filter_json


def _exact_keys(obj: Any, allowed: frozenset[str], required: frozenset[str], path: str) -> dict[str, Any]:
    """The mass-assignment guard (SECURITY-BASELINE §7).

    An unknown key is **named and rejected**, never dropped: a document that
    carried ``"workspace": "<someone else's id>"`` must fail loudly, and a user
    who mistyped a key deserves to hear which one.
    """
    if not isinstance(obj, dict):
        raise ConditionValidationError("expected an object", path=path, code="not_an_object")
    extra = sorted(str(key) for key in set(obj) - allowed)
    if extra:
        raise ConditionValidationError(f"unknown key(s): {', '.join(extra)}", path=path, code="unknown_key")
    missing = sorted(required - set(obj))
    if missing:
        raise ConditionValidationError(f"missing key(s): {', '.join(missing)}", path=path, code="missing_key")
    return obj


def _parse_group(document: Any, path: str = "") -> tuple[str, tuple[dict[str, Any], ...]]:
    body = _exact_keys(document, _ROOT_KEYS, _ROOT_KEYS, path)
    match = body["match"]
    if match not in MATCH_MODES:
        raise ConditionValidationError(
            f"match must be one of {', '.join(MATCH_MODES)}", path=f"{path}match", code="bad_match"
        )
    rules = body["rules"]
    if not isinstance(rules, list):
        raise ConditionValidationError("rules must be a list", path=f"{path}rules", code="not_a_list")
    if len(rules) > MAX_RULES:
        raise ConditionValidationError(
            f"a filter may hold at most {MAX_RULES} rules", path=f"{path}rules", code="too_many_rules"
        )
    return match, tuple(rules)


def _parse_rule(raw: Any, path: str) -> dict[str, Any]:
    """Structural and vocabulary checks. No database, no ORM, no exceptions.

    Everything a hostile document can carry dies here, which is what makes the
    "rejected before the ORM" test assertable by capturing queries.
    """
    body = _exact_keys(raw, _RULE_KEYS, _RULE_REQUIRED, path)

    source = body["source"]
    if source not in SOURCE_NAMES:
        raise ConditionValidationError("unknown source", path=f"{path}.source", code="unknown_source")

    key = body["key"]
    if not isinstance(key, str):
        raise ConditionValidationError("key must be a string", path=f"{path}.key", code="bad_key_type")
    if not key or len(key) > MAX_KEY_CHARS:
        raise ConditionValidationError("key is empty or too long", path=f"{path}.key", code="bad_key")

    op = body["op"]
    if not isinstance(op, str) or op not in ALL_OPS:
        raise ConditionValidationError("unknown operator", path=f"{path}.op", code="unknown_op")
    if op not in _legal_ops(source):
        raise ConditionValidationError(
            f"operator is not valid for a {source} rule", path=f"{path}.op", code="op_not_legal_for_source"
        )

    has_value = "value" in body
    if op in VALUELESS_OPS and has_value:
        raise ConditionValidationError("this operator takes no value", path=f"{path}.value", code="value_not_allowed")
    if op not in VALUELESS_OPS and not has_value:
        raise ConditionValidationError("this operator needs a value", path=f"{path}.value", code="value_required")
    return body


def _parse_key(key_kind: str, key: str, path: str) -> Any:
    if key_kind == KEY_UUID:
        try:
            return UUID(key)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConditionValidationError("key is not a valid id", path=f"{path}.key", code="bad_uuid") from exc
    if key_kind == KEY_SYSTEM_FIELD:
        if key not in SYSTEM_FIELDS:
            raise ConditionValidationError("no such contact field", path=f"{path}.key", code="unknown_key")
        return key
    if key not in Platform.values:
        raise ConditionValidationError("no such platform", path=f"{path}.key", code="unknown_key")
    return key


def _coerce_day(raw: Any, path: str) -> date | RelativeDay:
    """An ISO calendar date, or a relative offset. Day granularity only.

    SPEC §11.4's whole date vocabulary is day-shaped ("before/after/on", "days
    ago"), and one granularity is what makes ``before`` ∪ ``on`` ∪ ``after`` an
    exact partition in both evaluation modes. A full ISO timestamp is rejected
    rather than truncated.
    """
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ConditionValidationError(
                "expected a calendar date like 2026-08-21", path=f"{path}.value", code="bad_date"
            ) from exc
    body = _exact_keys(raw, frozenset({"relative"}), frozenset({"relative"}), f"{path}.value")
    inner = _exact_keys(
        body["relative"], frozenset({"unit", "offset"}), frozenset({"unit", "offset"}), f"{path}.value.relative"
    )
    if inner["unit"] not in RELATIVE_UNITS:
        raise ConditionValidationError(
            f"unit must be one of {', '.join(RELATIVE_UNITS)}", path=f"{path}.value.relative.unit", code="bad_unit"
        )
    offset = inner["offset"]
    # isinstance(True, int) is True, so bool has to be excluded explicitly.
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise ConditionValidationError(
            "offset must be a whole number of days", path=f"{path}.value.relative.offset", code="bad_value_type"
        )
    if abs(offset) > MAX_RELATIVE_OFFSET:
        raise ConditionValidationError(
            f"offset must be within {MAX_RELATIVE_OFFSET} days",
            path=f"{path}.value.relative.offset",
            code="offset_out_of_range",
        )
    return RelativeDay(unit=inner["unit"], offset=offset)


def _coerce_value(raw: Any, value_type: str, path: str) -> Any:
    """Turn a JSON value into something the ORM can bind, or refuse it.

    Messages name the expected type; they never echo the value, which for a
    custom field is contact PII heading for a log line and (from issue #25) an
    API error body.
    """
    if value_type == TYPE_BOOLEAN:
        if not isinstance(raw, bool):
            raise ConditionValidationError("expected true or false", path=f"{path}.value", code="bad_value_type")
        return raw
    if value_type == TYPE_NUMBER:
        # isinstance(True, int) again: without this, {"op": ">", "value": true}
        # would silently compile to `value_number > 1`.
        if isinstance(raw, bool) or not isinstance(raw, int | float | str):
            raise ConditionValidationError("expected a number", path=f"{path}.value", code="bad_value_type")
        try:
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise ConditionValidationError("expected a number", path=f"{path}.value", code="bad_value_type") from exc
        if not number.is_finite() or abs(number) > Decimal(MAX_ABS_NUMBER):
            raise ConditionValidationError("number is out of range", path=f"{path}.value", code="number_out_of_range")
        return number
    if value_type in (TYPE_DATE, TYPE_DATETIME):
        if not isinstance(raw, str | dict):
            raise ConditionValidationError("expected a date", path=f"{path}.value", code="bad_value_type")
        return _coerce_day(raw, path)
    if not isinstance(raw, str):
        raise ConditionValidationError("expected text", path=f"{path}.value", code="bad_value_type")
    if len(raw) > MAX_VALUE_CHARS:
        raise ConditionValidationError("text is too long", path=f"{path}.value", code="value_too_long")
    if "\x00" in raw:
        # Postgres text cannot hold a NUL; psycopg raises at execute time, which
        # would be a 500 from a value a stranger controls.
        raise ConditionValidationError("text contains a null byte", path=f"{path}.value", code="nul_byte")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConditionValidationError("text is not valid UTF-8", path=f"{path}.value", code="bad_encoding") from exc
    return raw


def _resolve_group(
    workspace: Any,
    document: Any,
    *,
    path: str,
    budget: _Budget,
    seen: tuple[UUID, ...],
    depth: int,
) -> Group:
    """Structural parse, then one batched lookup per source kind.

    Resolution is scoped, so another workspace's tag id is simply not in the
    result and fails as "unknown" rather than "forbidden" — SECURITY-BASELINE
    §1's no-existence-oracle rule, applied to a filter document.
    """
    match, raw_rules = _parse_group(document, path)
    budget.spend(len(raw_rules))

    parsed: list[tuple[int, str, dict[str, Any], Any]] = []
    for index, raw in enumerate(raw_rules):
        rule_path = f"{path}rules[{index}]"
        body = _parse_rule(raw, rule_path)
        target = _parse_key(_SOURCES[body["source"]].key_kind, body["key"], rule_path)
        parsed.append((index, rule_path, body, target))

    def ids_for(source: str) -> set[Any]:
        return {target for _, _, body, target in parsed if body["source"] == source}

    known_tags: set[Any] = set()
    if tag_ids := ids_for("tag"):
        known_tags = set(Tag.objects.for_workspace(workspace).filter(pk__in=tag_ids).values_list("pk", flat=True))

    field_types: dict[Any, str] = {}
    if field_ids := ids_for("custom_field"):
        field_types = dict(
            CustomField.objects.for_workspace(workspace).filter(pk__in=field_ids).values_list("pk", "type")
        )

    segment_filters: dict[Any, Any] = {}
    if segment_ids := ids_for("segment"):
        segment_filters = dict(
            Segment.objects.for_workspace(workspace).filter(pk__in=segment_ids).values_list("pk", "filter_json")
        )

    rules: list[Rule] = []
    for index, rule_path, body, target in parsed:
        source, op = body["source"], body["op"]
        value_type = ""
        group: Group | None = None

        if source == "tag":
            if target not in known_tags:
                raise ConditionValidationError("no such tag", path=f"{rule_path}.key", code="unknown_tag")
        elif source == "custom_field":
            value_type = field_types.get(target, "")
            if not value_type:
                raise ConditionValidationError("no such custom field", path=f"{rule_path}.key", code="unknown_field")
            if op not in OPS_BY_TYPE[value_type]:
                raise ConditionValidationError(
                    f"operator is not valid for a {value_type} field", path=f"{rule_path}.op", code="op_type_mismatch"
                )
        elif source == "system_field":
            value_type = SYSTEM_FIELDS[target].type
            if op not in OPS_BY_TYPE[value_type]:
                raise ConditionValidationError(
                    f"operator is not valid for {SYSTEM_FIELDS[target].label}",
                    path=f"{rule_path}.op",
                    code="op_type_mismatch",
                )
        elif source == "segment":
            if target not in segment_filters:
                raise ConditionValidationError("no such segment", path=f"{rule_path}.key", code="unknown_segment")
            if target in seen:
                chain_text = " -> ".join(str(item) for item in (*seen, target))
                raise ConditionValidationError(f"segment cycle: {chain_text}", path=rule_path, code="segment_cycle")
            if depth >= MAX_SEGMENT_DEPTH:
                raise ConditionValidationError(
                    f"segments may nest at most {MAX_SEGMENT_DEPTH} deep", path=rule_path, code="segment_too_deep"
                )
            group = _resolve_group(
                workspace,
                _load(segment_filters[target]),
                path=f"{rule_path}.segment.",
                budget=budget,
                seen=(*seen, target),
                depth=depth + 1,
            )

        value = _coerce_value(body["value"], value_type, rule_path) if op not in VALUELESS_OPS else None
        rules.append(
            Rule(
                index=index,
                source=source,
                op=op,
                key=body["key"],
                value=value,
                value_type=value_type,
                target_id=target if isinstance(target, UUID) else None,
                group=group,
            )
        )
    return Group(match=match, rules=tuple(rules))


def validate(workspace: Any, filter_json: Any, *, exclude_segment_id: Any = None) -> CompiledFilter:
    """Reject anything the compiler must never see. Raises :class:`ConditionValidationError`.

    ``exclude_segment_id`` seeds the cycle detector, so a segment being saved
    cannot reference itself even before it has a primary key on disk.

    Costs at most one query per source kind per group, regardless of rule count.
    """
    seen: tuple[UUID, ...] = (exclude_segment_id,) if exclude_segment_id else ()
    root = _resolve_group(workspace, _load(filter_json), path="", budget=_Budget(), seen=seen, depth=0)
    return CompiledFilter(root=root)


# ---------------------------------------------------------------------------
# Compilation — ORM expressions only, built from the allowlists above
# ---------------------------------------------------------------------------


class _Ctx:
    """Per-compilation state. The timezone is resolved lazily, so a filter with
    no date rule never pays for the workspace lookup."""

    def __init__(self, workspace: Any, now: datetime) -> None:
        self.workspace = workspace
        self.now = now
        self._tz: tzinfo | None = None

    @property
    def tz(self) -> tzinfo:
        if self._tz is None:
            self._tz = _workspace_timezone(self.workspace)
        return self._tz


def _workspace_timezone(workspace: Any) -> tzinfo:
    """The workspace's clock — ``Workspace.effective_timezone``, which already
    falls back to the organization's default.

    One timezone for the whole query, so set-wise evaluation stays a single
    statement. Per-contact boundaries would need ``col AT TIME ZONE
    contact.timezone`` in the WHERE clause, which no index can serve — and SPEC
    §11.4 pointedly lacks the ``use_contact_timezone`` flag §11.5 has.
    """
    from apps.workspaces.models import Workspace

    row: Workspace | None
    if isinstance(workspace, Workspace):
        row = workspace
    else:
        pk = getattr(workspace, "pk", workspace)
        row = Workspace.objects.select_related("organization").filter(pk=pk).first()
    name = row.effective_timezone if row is not None else settings.TIME_ZONE
    try:
        return ZoneInfo(name or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        # A workspace can hold any string; an unknown zone must not 500 a segment.
        return ZoneInfo(settings.TIME_ZONE)


def _lookup(column: str, suffix: str) -> str:
    """The **only** place in this module that builds a lookup string.

    ``column`` comes from :data:`SYSTEM_FIELDS` or from the model's own
    ``VALUE_COLUMNS`` table, and ``suffix`` from a frozen operator table.
    Neither is ever user input — which is what makes SECURITY-BASELINE §7's
    "field/operator allowlist, no string-built SQL" structural rather than a
    convention.
    """
    return f"{column}__{suffix}" if suffix else column


def _day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` covering ``day``.

    ``datetime.combine(..., tzinfo=tz)`` rather than ``make_aware``: midnight
    does not exist on some DST days (America/Santiago, Asia/Beirut) and
    ``make_aware`` raises there, which would turn a legal filter into a 500 on
    two days a year.
    """
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


_NUMBER_LOOKUPS: dict[str, str] = {"=": "exact", ">": "gt", "<": "lt", ">=": "gte", "<=": "lte"}

#: The two constant predicates, written as *real* predicates rather than as
#: ``Q()`` and its negation.
#:
#: A bare ``Q()`` is Django's identity element, not a "matches everyone" clause,
#: and it disappears in two directions that matter here. ``~Q()`` compiles to no
#: predicate at all — Django's ``WhereNode`` short-circuits a childless node
#: before it ever consults ``negated`` — so ``not_in`` a segment that matches
#: everyone would have matched everyone instead of nobody, silently dropping an
#: exclusion clause. And ``Q() | other`` collapses to ``other``, so an ``any``
#: group containing such a segment would have under-matched.
#:
#: ``pk__isnull=False`` is true for every persisted row and negates to
#: ``pk IS NULL``, which is true for none, so both directions behave. The cost
#: is one trivially-satisfiable clause in the SQL, and only in the degenerate
#: case that produces it.
_EVERYONE = Q(pk__isnull=False)
_NOBODY = Q(pk__in=())


def _populated_q(column: str, value_type: str) -> Q:
    """ "Has a value" — for text that means non-empty, not merely non-NULL."""
    present = Q(**{_lookup(column, "isnull"): False})
    return present & ~Q(**{column: ""}) if value_type == TYPE_TEXT else present


def _value_q(column: str, value_type: str, op: str, value: Any, ctx: _Ctx) -> Q:
    """The positive predicate for one comparison. Negatives are ``~`` of this.

    Serves both evaluation surfaces: on ``Contact`` columns for ``system_field``
    and on ``CustomFieldValue`` columns inside the ``Exists()`` subquery for
    ``custom_field``. One table, so the two can never diverge.
    """
    if value_type == TYPE_TEXT:
        # Case-insensitive: operators type "vip" and the tag reads "VIP". At 10k
        # rows per workspace the ILIKE scan is milliseconds; issue #13's
        # interactive search box is the consumer that will justify a trigram index.
        return Q(**{_lookup(column, "icontains" if op == "contains" else "iexact"): value})
    if value_type == TYPE_NUMBER:
        return Q(**{_lookup(column, _NUMBER_LOOKUPS[op]): value})
    if value_type == TYPE_BOOLEAN:
        return Q(**{_lookup(column, "exact"): value})

    day = value.resolve(ctx.now, ctx.tz) if isinstance(value, RelativeDay) else value
    if value_type == TYPE_DATE:
        return Q(**{_lookup(column, {"before": "lt", "on": "exact", "after": "gt"}[op]): day})
    # A datetime column compared against a day. Half-open ranges rather than
    # __date=, which compiles to a cast Postgres cannot answer from a btree.
    start, end = _day_bounds(day, ctx.tz)
    if op == "before":
        return Q(**{_lookup(column, "lt"): start})
    if op == "after":
        return Q(**{_lookup(column, "gte"): end})
    return Q(**{_lookup(column, "gte"): start, _lookup(column, "lt"): end})


def _definite(q: Q, *, column: str, nullable: bool) -> Q:
    """Make a predicate two-valued so ``~q`` is an exact complement.

    ``NOT (col < X)`` is NULL — not TRUE — for a row where ``col`` is NULL, so
    such a row would fall out of *both* halves of a pair and the two would stop
    partitioning the workspace.
    """
    return q & Q(**{_lookup(column, "isnull"): False}) if nullable else q


def _tag_q(ctx: _Ctx, rule: Rule) -> Q:
    # for_workspace, not filter: the scoping guard does not fire inside Exists(),
    # so this predicate is the subquery's only tenancy check.
    rows = ContactTag.objects.for_workspace(ctx.workspace).filter(contact=OuterRef("pk"), tag_id=rule.target_id)
    present = Q(Exists(rows))
    # has_not is NOT EXISTS, so it is true for a contact with no tag rows at all.
    return present if rule.op == "has" else ~present


def _custom_field_q(ctx: _Ctx, rule: Rule) -> Q:
    column = VALUE_COLUMNS[rule.value_type]
    rows = CustomFieldValue.objects.for_workspace(ctx.workspace).filter(contact=OuterRef("pk"), field_id=rule.target_id)
    positive_op = _POSITIVE_OF.get(rule.op, rule.op)
    if positive_op == "has_value":
        inner = _populated_q(column, rule.value_type)
    else:
        inner = _value_q(column, rule.value_type, positive_op, rule.value, ctx)
    present = Q(Exists(rows.filter(inner)))
    # A contact with no row at all matches every negative op. See the module
    # docstring: that is what makes each pair partition the workspace.
    return present if positive_op == rule.op else ~present


def _system_field_q(ctx: _Ctx, rule: Rule) -> Q:
    spec = SYSTEM_FIELDS[rule.key]
    positive_op = _POSITIVE_OF.get(rule.op, rule.op)
    if positive_op == "has_value":
        positive = _populated_q(spec.column, spec.type)
    else:
        positive = _value_q(spec.column, spec.type, positive_op, rule.value, ctx)
    positive = _definite(positive, column=spec.column, nullable=spec.nullable)
    return positive if positive_op == rule.op else ~positive


def _segment_q(ctx: _Ctx, rule: Rule) -> Q:
    # The referenced segment was validated and resolved by validate(), so this
    # is a plain recursion: its Q is inlined into the same WHERE clause rather
    # than becoming a subquery, and the whole filter stays one statement.
    inner = _compile_group(ctx, rule.group) if rule.group is not None else _EVERYONE
    return inner if rule.op == "in" else ~inner


def _slot_q(_ctx: _Ctx, rule: Rule) -> Q:
    raise SourceNotEvaluableError(
        f"The {rule.source!r} condition source is declared but not implemented in this deployment "
        f"({_SOURCES[rule.source].owner}). A filter using it validates and can be saved, but cannot "
        f"be evaluated until that issue lands."
    )


def _compile_rule(ctx: _Ctx, rule: Rule) -> Q:
    source = _SOURCES[rule.source]
    if source.build_q is None:
        return _slot_q(ctx, rule)
    return source.build_q(ctx, rule)


def _compile_group(ctx: _Ctx, group: Group) -> Q:
    parts = [_compile_rule(ctx, rule) for rule in group.rules]
    if not parts:
        # Identity elements: `all` with no rules is everyone (identity of AND),
        # `any` with no rules is nobody. Correct algebra and a live hazard — an
        # empty segment handed to a broadcast targets the whole workspace, so
        # issue #23 must show a count before it sends.
        return _EVERYONE if group.match == MATCH_ALL else _NOBODY
    return reduce(operator.and_ if group.match == MATCH_ALL else operator.or_, parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def queryset(workspace: Any, filter_json: Any, *, now: datetime | None = None) -> QuerySet[Contact]:
    """The contacts in ``workspace`` matching ``filter_json``.

    Lazy: the returned queryset is a single SQL statement, so a caller may count
    it, slice it or iterate it. ``filter_json`` may be a raw document or a
    :class:`CompiledFilter` from a previous :func:`validate`.
    """
    compiled = filter_json if isinstance(filter_json, CompiledFilter) else validate(workspace, filter_json)
    ctx = _Ctx(workspace, now or timezone.now())
    return (
        Contact.objects.for_workspace(workspace)
        # Filter in rather than exclude out: a status added later is excluded by
        # default, which is the right default for anything feeding a send path.
        .filter(status=ContactStatus.ACTIVE)
        .filter(_compile_group(ctx, compiled.root))
    )


def evaluate(contact: Contact, filter_json: Any, *, now: datetime | None = None) -> bool:
    """Whether ``contact`` is in the set :func:`queryset` would return.

    One implementation of the operator semantics, one query. A second, in-Python
    evaluator would mean writing the absence rules, the day boundaries and the
    match algebra twice, and a disagreement between the two would show up as a
    contact receiving a message their segment says they will not receive.
    """
    if contact.pk is None:
        raise ValueError("evaluate() needs a saved contact.")
    return queryset(contact.workspace_id, filter_json, now=now).filter(pk=contact.pk).exists()


def evaluate_many(
    workspace: Any, contacts: Iterable[Contact], filter_json: Any, *, now: datetime | None = None
) -> set[Any]:
    """Which of ``contacts`` match — one query for the whole batch."""
    ids = [contact.pk for contact in contacts]
    if not ids:
        return set()
    return set(queryset(workspace, filter_json, now=now).filter(pk__in=ids).values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# Declarations. All six sources exist here so CONDITION_SCHEMA cannot depend on
# import order; the two slots get their build_q from register_source() later.
# ---------------------------------------------------------------------------

for _declaration in (
    ConditionSource("tag", SOURCE_LABELS["tag"], KEY_UUID, OPS_BY_SOURCE["tag"], _tag_q),
    ConditionSource("custom_field", SOURCE_LABELS["custom_field"], KEY_UUID, tuple(sorted(TYPE_OPS)), _custom_field_q),
    ConditionSource(
        "system_field", SOURCE_LABELS["system_field"], KEY_SYSTEM_FIELD, tuple(sorted(TYPE_OPS)), _system_field_q
    ),
    ConditionSource("segment", SOURCE_LABELS["segment"], KEY_UUID, OPS_BY_SOURCE["segment"], _segment_q),
    ConditionSource(
        "sequence", SOURCE_LABELS["sequence"], KEY_UUID, OPS_BY_SOURCE["sequence"], None, "issue #22, L6-A"
    ),
    ConditionSource("window", SOURCE_LABELS["window"], KEY_PLATFORM, OPS_BY_SOURCE["window"], None, "issue #8, L3-A"),
):
    _SOURCES[_declaration.name] = _declaration
del _declaration

CONDITION_SCHEMA: dict[str, Any] = _build_schema()
