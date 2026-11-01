"""The migrated i18n surface stays translatable.

Not a hunt for every hardcoded string in the product — most of templates/ and
apps/ that carries no user-facing copy is not tracked here at all, by
construction: this test only ever looks at files the committed translation
catalog already references, never at a hand-maintained list of "migrated"
files.

That is the upgrade this file used to promise in a comment: a fixed
``_I18N_TEMPLATES``/``_I18N_MODULES`` allowlist, edited once per phase, whose
presence-of-``{% load i18n %}`` check would still pass if every
``{% translate %}`` inside a file were quietly deleted around it. Instead,
``locale/ru/LC_MESSAGES/django.po``'s own ``#: path:line`` comments — written
by ``make i18n-extract``, which every phase in this rollout has run and
committed before merging — are the ground truth for "which files call
translate, and how many times". This test re-scans each of those files and
fails if any of them now calls translate *fewer* times than the catalog
expects: the signature of a wrap being deleted, whether by an accidental
revert or an edit that skated past the string next to it.

# ponytail: a call-site *count* per file, from a regex over each gettext-family
# call shape actually used in this repo (`gettext`/`gettext_lazy`/`ngettext`/
# `pgettext_lazy`/bare `_(`/`{% translate %}`/`{% blocktranslate %}`) — not a
# byte-for-byte extraction diff. Cheap and dependency-free, and asymmetric in
# the safe direction: a call shape this regex does not recognise only ever
# under-counts what is *actually* there, which cannot produce a false pass
# (the assertion fails on too few, never on too many) — and every shape this
# repo's own gettext calls currently take was read off the source before
# writing the pattern, not guessed. What it cannot catch, by construction, is
# new English prose added and never wrapped in the first place: extraction
# only ever sees what somebody already wrapped, so this test's ceiling is a
# regression in already-migrated copy, not a hunt for the next migration.
# Upgrade to shelling out to `make i18n-extract` and diffing its output
# against `git`'s tracked locale/ if that gap ever actually bites.
"""

import re
from pathlib import Path

from django.conf import settings

#: One `#:` comment's source references, e.g. `apps/api/events.py:94 apps/api/events.py:95`.
_SOURCE_REFS = re.compile(r"^#: (.+)$", re.MULTILINE)

#: Every call shape this repo's gettext-family imports actually take (verified
#: against the source, not the gettext/Django docs' full vocabulary — see the
#: module docstring's ponytail note). The bare `_(` alternative excludes a
#: preceding word character or dot, so `self._foo(` and `obj._(` do not count.
_TRANSLATE_CALL = re.compile(
    r"\{%\s*(?:blocktranslate|translate)\b"
    r"|\b(?:gettext|ngettext)(?:_lazy)?\s*\("
    r"|\bpgettext(?:_lazy)?\s*\("
    r"|(?<![\w.])_\s*\("
)


def _expected_call_counts(po_path: Path) -> dict[str, int]:
    """``{relative path: how many translate call-sites the catalog expects}``.

    Counts *references*, not distinct msgids — two calls sharing identical
    text collapse to one catalog entry but keep two ``#:`` lines, one per call
    site, so counting references stays one-to-one with what the regex above
    counts in the source.
    """
    counts: dict[str, int] = {}
    for line in _SOURCE_REFS.findall(po_path.read_text()):
        for ref in line.split():
            path = ref.rsplit(":", 1)[0]
            counts[path] = counts.get(path, 0) + 1
    return counts


def test_no_migrated_file_calls_translate_less_than_the_catalog_expects():
    root = Path(settings.BASE_DIR)
    expected = _expected_call_counts(root / "locale/ru/LC_MESSAGES/django.po")
    assert len(expected) > 150, (
        "far fewer source files than expected are referenced in the catalog — "
        "this probably means locale/ru/LC_MESSAGES/django.po itself failed to "
        "load rather than that the product shrank"
    )

    shrunk = {}
    for rel_path, expected_count in expected.items():
        path = root / rel_path
        if not path.is_file():
            # A stale reference to a file that was deleted outright is a
            # `make i18n-extract` hygiene question for whoever removed it, not
            # a "translate call went missing" regression — nothing to count.
            continue
        actual_count = len(_TRANSLATE_CALL.findall(path.read_text()))
        if actual_count < expected_count:
            shrunk[rel_path] = {"expected_at_least": expected_count, "found": actual_count}

    assert shrunk == {}, (
        f"these files call translate()/{{% translate %}} fewer times than the committed "
        f"catalog's own #: references expect — a wrap was likely deleted without "
        f"re-running `make i18n-extract`: {shrunk}"
    )
