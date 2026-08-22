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
    "parse_filter_document",
    "resolve_query",
    "search",
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

    The byte cap is checked **before** ``json.loads``, on the encoded length, for
    the reason :func:`apps.contacts.conditions._load` gives at length: a cap
    applied after parsing has already paid for the parse it exists to prevent.
    ``conditions.validate`` re-applies its own caps to the result — this is the
    cheap outer gate, not the security boundary.

    An empty value is an empty document ("no filter"), not an error: the list
    page links to itself with ``?filter=`` in the query string whether or not a
    filter is set.
    """
    if not raw:
        return {}
    if not isinstance(raw, str):
        raise ConditionValidationError("filter must be text", code="bad_json")
    if len(raw.encode("utf-8", "surrogatepass")) > conditions.MAX_FILTER_BYTES:
        raise ConditionValidationError("filter document is too large", code="too_large")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ConditionValidationError("filter document is not valid JSON", code="bad_json") from exc
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
    error = ""
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

    return search(rows, query.search_term).order_by(*SORTS[query.sort]), error
