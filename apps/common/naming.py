"""Naming a workspace-scoped row: clean it, check it is free, survive the race.

Three helpers that every "things in a workspace have unique names" table needs,
and that three apps had started to grow their own copies of. ``apps.contacts``
uses them for tags, custom fields and segments; ``apps.campaigns`` for
sequences. The reasoning below is the whole reason they are shared rather than
re-derived per app — each one encodes a trap, and a second copy is a second
place to fix it.

Each takes the caller's own ``error`` class so a refusal still arrives as that
app's ``ValueError`` subclass, which is what lets views keep catching one type
and rendering ``messages.error(request, str(exc))``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.db import IntegrityError, transaction

__all__ = ["assert_name_is_free", "clean_name", "unique_name"]


def clean_name(value: Any, *, limit: int, noun: str, error: type[Exception]) -> str:
    """Collapse whitespace and **refuse** anything over the column width.

    Silently truncating a name a human typed is worse than refusing it twice
    over: they see a name they did not type, and two names that differ only
    after the limit collapse into one, so the second attempt is rejected as a
    duplicate of a name that looks nothing like it on screen.

    A NUL is refused rather than stripped: Postgres text cannot hold one and
    ``split()`` does not treat it as whitespace, so without this it survives
    into ``objects.create()`` and psycopg raises at execute time — a 500 for a
    value a stranger supplied.
    """
    if not isinstance(value, str) and value is not None:
        raise error(f"A {noun} name must be text.")
    if value and "\x00" in value:
        raise error(f"A {noun} name cannot contain a null byte.")
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise error(f"A {noun} needs a name.")
    if len(cleaned) > limit:
        raise error(f"A {noun} name is at most {limit} characters.")
    return cleaned


def assert_name_is_free(
    model: Any, workspace: Any, name: str, *, noun: str, error: type[Exception], excluding: Any = None
) -> None:
    """Refuse a name another row in the workspace already holds.

    Matched case-insensitively, because these unique constraints are on
    ``Lower(name)`` — checking with ``=`` here would let "vip" through and then
    let the database raise on it.
    """
    rows = model.objects.for_workspace(workspace).filter(name__iexact=name)
    if excluding is not None:
        rows = rows.exclude(pk=excluding)
    if rows.exists():
        raise error(f"A {noun} with that name already exists.")


@contextmanager
def unique_name(noun: str, *, error: type[Exception]) -> Iterator[None]:
    """Turn the unique-index violation the check above races with into a refusal.

    :func:`assert_name_is_free` is a check-then-write, so two concurrent
    requests can both pass it. Without this the loser gets an ``IntegrityError``
    — a 500 for input the single-threaded path answers with a readable message —
    and poisons any enclosing atomic block. The savepoint keeps that block
    usable.

    It also covers the case ``full_clean()`` cannot: a constraint written as an
    expression over ``Lower(name)`` **and** ``workspace`` is skipped by a
    ``full_clean`` that excludes the derived ``workspace`` field.
    """
    try:
        with transaction.atomic():
            yield
    except IntegrityError as exc:
        raise error(f"A {noun} with that name already exists.") from exc
