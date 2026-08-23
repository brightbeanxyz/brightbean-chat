"""Turning a request's query string into a scoped contact queryset.

Four surfaces ask the same question — the list page, its htmx rows partial, the
CSV export and the bulk-action endpoint — and they have to agree, because the
export's promise is "the rows you are looking at" and a bulk action's is "the
rows you ticked out of the rows you are looking at". Two implementations of that
is two answers.

So the request is parsed once, here, into a :class:`ContactQuery`, and the
queryset is built once from it.

--------------------------------------------------------------------------
Three narrowings, and why only one of them is the condition engine's
--------------------------------------------------------------------------

* ``?filter=`` is a SPEC §11.4 filter document and goes to
  :mod:`apps.contacts.conditions` untouched. **Set-wise, through the ORM** — the
  engine compiles one statement, and a Python loop over contacts would both
  scale badly and re-implement the operator semantics the engine owns.
* ``?segment=`` names a saved filter. It is the same document, fetched from a
  row instead of the query string.
* ``?q=`` and ``?sort=`` are not filter rules at all and deliberately do not
  become them. Search is a UI affordance over four columns, not a segmentable
  predicate, and ordering is not a predicate in any sense; pushing either into
  ``filter_json`` would put things in saved segments that the flow Condition
  node would then have to know how to evaluate.

--------------------------------------------------------------------------
Why a bad filter fails closed
--------------------------------------------------------------------------

A filter that no longer compiles — a segment saved before someone deleted the
tag it names, or one using a source this deployment cannot evaluate yet — yields
**no contacts** plus an explanation, never the unfiltered workspace. The operator
asked for a subset; answering with everyone is the least safe way to be wrong,
and the count they read off the page would be wrong in the direction that feeds
a bulk delete.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from django.db.models import F, Q, QuerySet

from apps.common.shortcuts import get_scoped_object_or_404
from apps.contacts import conditions
from apps.contacts.conditions import ConditionError, ConditionValidationError
from apps.contacts.models import Contact, ContactStatus, Segment

__all__ = [
    "DEFAULT_SORT",
    "MAX_SEARCH_CHARS",
    "SORTS",
    "ContactQuery",
    "contacts_for",
    "filter_config",
    "parse_filter_document",
    "resolve_query",
    "search",
    "sequence_options",
]

#: The orderings the list offers. The request supplies a **key into this dict**,
#: never an ``order_by`` argument — which is what keeps a user string out of the
#: ORM's field resolution, the same discipline
#: ``apps.contacts.conditions._lookup`` follows for operators.
#:
#: ``-id`` closes every one of them. Primary keys are UUIDv7, so it is a stable,
#: unique, index-backed tiebreak that costs nothing — and without a total order a
#: paginator silently repeats and drops rows between pages when the leading
#: column ties.
SORTS: dict[str, tuple[Any, ...]] = {
    # nulls_last is load-bearing: Postgres sorts NULL above every value under
    # DESC, so a plain "-last_interaction_at" puts every contact who has never
    # interacted at the top of a list whose whole point is recency.
    "recent": (F("last_interaction_at").desc(nulls_last=True), "-id"),
    "oldest": (F("last_interaction_at").asc(nulls_first=True), "id"),
    "name": ("first_name", "last_name", "-id"),
    "name_desc": ("-first_name", "-last_name", "-id"),
    "email": ("email", "-id"),
    "created": ("-created_at", "-id"),
}

DEFAULT_SORT = "recent"

#: Cap on the search box. Longer than any name and short enough that the ILIKE
#: it becomes cannot be used to hand Postgres a megabyte to compare per row.
MAX_SEARCH_CHARS = 200


@dataclass(frozen=True)
class ContactQuery:
    """One parsed request. Everything a caller needs to render or re-link.

    ``document`` is the filter as it should be echoed back into the builder, so
    a round-trip through the page cannot normalise a saved segment into
    something slightly different from what is on disk.
    """

    document: dict[str, Any] = field(default_factory=dict)
    segment: Segment | None = None
    search_term: str = ""
    sort: str = DEFAULT_SORT
    #: Non-empty when the filter could not be compiled. Rendered as a warning
    #: beside an empty list.
    error: str = ""

    @property
    def is_filtered(self) -> bool:
        return bool(self.document) or bool(self.search_term)

    @property
    def raw_filter(self) -> str:
        """The filter as a query-string value, for pagination and export links."""
        return json.dumps(self.document, separators=(",", ":")) if self.document else ""


def parse_filter_document(raw: Any) -> dict[str, Any]:
    """Parse a ``?filter=`` value into a filter document, or refuse it.

    Straight through to :func:`apps.contacts.conditions.load_document`, which is
    the engine's own loader, and that delegation is the point rather than a
    convenience. This function used to call ``json.loads`` itself behind a byte
    cap, and a byte cap does not close the **depth** hole: 16 KiB of ``[`` fits
    inside ``MAX_FILTER_BYTES`` and blows CPython's recursion limit, and the
    ``RecursionError`` that comes back is not a ``ValueError``, so it escaped
    this function as a 500 rather than a refusal. ``load_document`` measures
    bracket depth before it parses, catches ``RecursionError`` alongside
    ``ValueError``, rejects bare ``NaN``/``Infinity``, and re-applies the node and
    size caps to the parsed result — all of which its docstring explains, and
    none of which is worth a second implementation here.

    An empty value is an empty document ("no filter"), not an error: the list
    page links to itself with ``?filter=`` in the query string whether or not a
    filter is set.
    """
    if not raw:
        return {}
    if not isinstance(raw, str):
        raise ConditionValidationError("filter must be text", code="bad_json")
    document = conditions.load_document(raw)
    if not isinstance(document, dict):
        raise ConditionValidationError("filter document must be an object", code="not_an_object")
    return document


def search(queryset: QuerySet[Contact], term: str) -> QuerySet[Contact]:
    """Case-insensitive match across the four columns a human searches by.

    ORM ``icontains`` rather than Postgres full-text search, and the trade is the
    same one ``apps.media_library.filters.search`` documents: stemming and
    ranking buy nothing over names and addresses, and the leading-wildcard scan
    is milliseconds at the ten thousand rows per workspace this list is sized
    for. ``apps.contacts.conditions`` already names this box as the thing that
    will eventually justify a trigram index — when it does, it is an index and a
    migration, not a rewrite of this function.
    """
    term = term.strip()[:MAX_SEARCH_CHARS]
    if not term:
        return queryset
    return queryset.filter(
        Q(first_name__icontains=term)
        | Q(last_name__icontains=term)
        | Q(email__icontains=term)
        | Q(phone__icontains=term)
    )


def resolve_query(request: Any, workspace: Any) -> ContactQuery:
    """Read ``?filter=``, ``?segment=``, ``?q=`` and ``?sort=`` off a request.

    ``?segment=`` wins over ``?filter=`` when both are present: loading a segment
    is an explicit act, and the stale ``filter`` left in the URL by the control
    that loaded it is not.

    The segment id goes through ``get_scoped_object_or_404``, so another
    workspace's id is a 404 rather than an empty list — the same answer every
    other id in this app gives (SECURITY-BASELINE §1). **The IDOR sweep cannot
    see this**: it walks URL kwargs, not the query string, so the cross-tenant
    case has its own test in ``apps/contacts/tests/test_views.py``.
    """
    params = request.GET
    sort = params.get("sort") or DEFAULT_SORT
    if sort not in SORTS:
        sort = DEFAULT_SORT
    term = (params.get("q") or "").strip()[:MAX_SEARCH_CHARS]

    segment: Segment | None = None
    segment_id = params.get("segment") or ""
    if segment_id:
        segment = get_scoped_object_or_404(Segment, workspace, pk=segment_id)
        document: dict[str, Any] = segment.filter_json if isinstance(segment.filter_json, dict) else {}
        return ContactQuery(document=document, segment=segment, search_term=term, sort=sort)

    try:
        document = parse_filter_document(params.get("filter"))
    except ConditionValidationError as exc:
        return ContactQuery(search_term=term, sort=sort, error=str(exc))
    return ContactQuery(document=document, search_term=term, sort=sort)


def contacts_for(workspace: Any, query: ContactQuery) -> tuple[QuerySet[Contact], str]:
    """``(queryset, error)`` — the contacts this request is asking about.

    The error is returned rather than raised because the page renders it: an
    unusable segment is something the operator has to be told about beside an
    empty table, not a 500 and not a silent full list. See the module docstring
    on failing closed.
    """
    if query.error:
        return Contact.objects.for_workspace(workspace).none(), query.error

    rows: QuerySet[Contact]
    if query.document:
        try:
            rows = conditions.queryset(workspace, query.document)
        except ConditionError as exc:
            return Contact.objects.for_workspace(workspace).none(), str(exc)
    else:
        # Filter in rather than exclude out, matching conditions.queryset: a
        # status added later is excluded by default, which is the right default
        # for anything that can feed a bulk action.
        rows = Contact.objects.for_workspace(workspace).filter(status=ContactStatus.ACTIVE)

    # Every failure above returns early, so reaching here means there is nothing
    # to report — the empty string is the answer, not a variable's last value.
    return search(rows, query.search_term).order_by(*SORTS[query.sort]), ""


def filter_config(workspace: Any, *, document: Any = None, segment_id: str = "") -> dict[str, Any]:
    """Everything the §11.4 filter builder needs to render, in one payload.

    ``CONDITION_SCHEMA["x-brightbean"]`` already carries the operator tables, the
    valueless-operator set, the operator labels, the system fields, the relative
    units, which sources this deployment cannot evaluate, and the limits — that
    extension block exists precisely so a consumer does not have to keep a second
    copy. So the builder reads it, and an operator added to
    :mod:`apps.contacts.conditions` shows up in the UI with no edit here.

    Only the things that cannot live in a static schema are added: each source's
    label, evaluability and owning issue from the registry, and this workspace's
    own tags, fields, segments and sequences.

    Public and in this module rather than private to a view, because two surfaces
    render the same builder: the CRM's filter bar and — since L6-A — the rule
    trigger's panel in the flow builder's trigger drawer. One payload shape means
    ``templates/contacts/_filter_bar.html`` serves both.

    One dict rather than six template variables, because it is one ``x-data``
    argument, and assembling it in the template would put the payload's shape
    somewhere Python cannot see it.
    """
    from apps.common.platforms import Platform
    from apps.contacts.conditions import CONDITION_SCHEMA
    from apps.contacts.models import CustomField, Tag

    registry = conditions.sources()
    return {
        "sources": [
            {
                "name": name,
                "label": registry[name].label,
                "keyKind": registry[name].key_kind,
                "evaluable": registry[name].is_evaluable,
                # Carried so a greyed-out row can say *why* it is unavailable.
                "owner": registry[name].owner,
            }
            for name in conditions.SOURCE_NAMES
        ],
        "vocabulary": CONDITION_SCHEMA["x-brightbean"],
        "platforms": [{"value": value, "label": label} for value, label in Platform.choices],
        "tags": [
            {"value": str(row.pk), "label": row.name} for row in Tag.objects.for_workspace(workspace).order_by("name")
        ],
        "fields": [
            {"value": str(row.pk), "label": row.name, "type": row.type}
            for row in CustomField.objects.for_workspace(workspace).order_by("name")
        ],
        "segments": [
            {"value": str(row.pk), "label": row.name}
            for row in Segment.objects.for_workspace(workspace).order_by("name")
        ],
        "sequences": sequence_options(workspace),
        # The document the builder hydrates from. Handed in by the caller rather
        # than re-read from the URL, so a segment loaded off disk round-trips
        # exactly as stored instead of through a re-serialisation that could
        # normalise it.
        "document": document if isinstance(document, dict) else {},
        "segmentId": segment_id,
    }


def sequence_options(workspace: Any, *, enrollable: bool = False) -> list[dict[str, str]]:
    """This workspace's sequences, for a picker.

    ``enrollable`` narrows it to the ones that would actually accept a
    subscriber — the ``active`` ones, which is what
    ``apps.campaigns.services.subscribe`` enforces. The two callers want
    genuinely different sets and getting them the same way round is a bug in
    each direction:

    * the **enrolment** controls (the CRM's bulk action, the contact pane) must
      not offer a sequence every attempt would refuse;
    * the **condition** key picker must offer every sequence, because "not
      subscribed to the old onboarding" is a perfectly good segment rule about a
      campaign that was archived last year.

    Resolved through Django's app registry rather than by importing
    ``apps.campaigns``: this app is L2-A and campaigns is L6-A, and a lower layer
    importing a higher one is the coupling ``apps/flows/compat.py`` exists to
    avoid on the same question. The registry is the neutral seam, and a
    deployment without the app installed simply gets no options — which is
    exactly the state the filter bar shipped in until #22.
    """
    from django.apps import apps as django_apps

    try:
        model = django_apps.get_model("campaigns", "Sequence")
    except LookupError:
        return []
    rows = model.objects.for_workspace(workspace)
    if enrollable:
        rows = rows.filter(status="active")
    return [{"value": str(row["id"]), "label": row["name"]} for row in rows.order_by("name").values("id", "name")]
