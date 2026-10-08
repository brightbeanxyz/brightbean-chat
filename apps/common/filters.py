"""Reading a list page's filters out of the query string.

Every list page used to read its filters with ``request.GET.get(name)`` — one
value per filter, validated by hand against that page's own enum. The redesign's
Filter popover (templates/components/filter_popover.html) lets a reader pick
several values in one group, "Live and Offline", which arrive as a repeated
parameter: ``?status=active&status=offline``. ``GET.get`` silently keeps only
the last of those, so the page would show half of what was asked for.

``multi`` is the one way to read such a parameter. It keeps every value, drops
anything it does not recognise (a stale bookmark, a hand-edited URL) rather than
failing the page, and returns the survivors deduplicated and sorted, so two URLs
that ask for the same thing in a different order produce the same list — which
is what a polling ETag built from it needs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from uuid import UUID

from django.http import QueryDict

#: A ceiling on how many values one parameter may carry. Generous for any real
#: filter (nobody picks fifty labels), and it keeps a crafted URL from turning
#: one request into a thousand-term IN clause.
MAX_VALUES = 50


def multi[T](
    params: QueryDict,
    name: str,
    *,
    allowed: Iterable[str] | None = None,
    parse: Callable[[str], T | None] | None = None,
) -> list[T] | list[str]:
    """Every recognised value of the repeated parameter ``name``.

    Args:
      params   ``request.GET``.
      name     the parameter, e.g. ``"status"``.
      allowed  the values that mean something — an enum's ``.values``. Others
               are dropped.
      parse    turns one raw value into the value the query wants (a UUID, an
               int), returning ``None`` for one that does not parse. Dropped
               values never reach the queryset. Mutually exclusive with
               ``allowed``.

    Blank values are ignored, so an empty hidden input — which is how a form
    says "no filter" — reads as no filter rather than as a filter on "".
    """
    if allowed is not None and parse is not None:
        raise ValueError("multi() takes allowed= or parse=, not both")
    raw = [value.strip() for value in params.getlist(name)[:MAX_VALUES]]
    raw = [value for value in raw if value]
    if parse is not None:
        parsed = {value for value in map(parse, raw) if value is not None}
        return sorted(parsed, key=str)
    if allowed is not None:
        known = {str(value) for value in allowed}
        raw = [value for value in raw if value in known]
    return sorted(set(raw))


def parse_uuid(value: str) -> UUID | None:
    """``parse=`` for an id parameter: a UUID, or ``None`` for anything else."""
    try:
        return UUID(value)
    except ValueError:
        return None
