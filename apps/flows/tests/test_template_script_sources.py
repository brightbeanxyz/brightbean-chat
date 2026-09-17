"""The other half of the library is what its script generates.

``apps/flows/tests/test_template_library_sources.py`` gates the templates whose
definitions live in :mod:`apps.flows.tests.template_sources`. The rest of
``flow-templates/`` is written by ``scripts/make_flow_templates.py``, which is
where the module docstring tells contributors to author a new one — and which,
until this module existed, nothing ran. Two generators, one directory, and a
gate over only half of it meant a hand-edit to twenty-four files was invisible
to CI.

The two partition the directory: ``test_the_two_generators_do_not_overlap``
below is what keeps that true, because a file claimed by both would be rewritten
by whichever ran last and neither gate would agree with the other.

Regenerates the same way its sibling does::

    BRIGHTBEAN_REGENERATE_TEMPLATES=1 pytest apps/flows/tests/test_template_script_sources.py
"""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.flows.portability import export_document, serialize
from apps.flows.portability.library import library_path, template_paths
from apps.flows.tests.template_sources import DEFINITIONS
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db

REGENERATE = os.environ.get("BRIGHTBEAN_REGENERATE_TEMPLATES") == "1"

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "make_flow_templates.py"


def _script() -> Any:
    """The authoring script, imported as a module.

    It is not on the import path and is written to be run, so it is loaded by
    location. Its ``django.setup()`` at import is a no-op under pytest, which
    has already configured settings — and its ``os.environ.setdefault`` leaves
    the test settings alone rather than swapping in development's.
    """
    if "make_flow_templates" in sys.modules:
        return sys.modules["make_flow_templates"]
    spec = importlib.util.spec_from_file_location("make_flow_templates", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_flow_templates"] = module
    spec.loader.exec_module(module)
    return module


def _slugs() -> list[str]:
    return [spec["slug"] for spec in _script().TEMPLATES]


class TestTheScriptGeneratedTemplates:
    def test_the_script_still_writes_every_file_it_claims(self) -> None:
        missing = [slug for slug in _slugs() if not (library_path() / f"{slug}.json").exists()]

        assert missing == [], f"the script claims templates that are not in the library: {missing}"

    @pytest.mark.parametrize("slug", _slugs() if SCRIPT.exists() else [])
    def test_each_file_is_what_the_script_generates(self, slug: str) -> None:
        script = _script()
        spec = next(item for item in script.TEMPLATES if item["slug"] == slug)
        tenancy = create_tenancy(slug=f"scr-{slug[:20]}")
        generated = serialize(export_document(script.build(tenancy.workspace, spec)))
        path = library_path() / f"{slug}.json"

        if REGENERATE:
            path.write_text(generated, encoding="utf-8")
            return

        assert path.read_text(encoding="utf-8") == generated, (
            f"{slug}.json is stale. Regenerate with BRIGHTBEAN_REGENERATE_TEMPLATES=1 pytest {Path(__file__).name}"
        )


class TestTheTwoGeneratorsPartitionTheLibrary:
    def test_the_two_generators_do_not_overlap(self) -> None:
        """A file both claim is rewritten by whichever ran last, and then one of
        the two gates is red for a reason that has nothing to do with the edit
        that turned it red. That is how this merge started."""
        script = set(_slugs())
        definitions = {source.filename[: -len(".json")] for source in DEFINITIONS}

        assert script & definitions == set()

    def test_every_shipped_template_has_a_generator(self) -> None:
        """The gap this module closes: a file with no generator has no staleness
        gate, so a hand-edit to it is invisible to CI."""
        script = set(_slugs())
        definitions = {source.filename[: -len(".json")] for source in DEFINITIONS}
        shipped = {path.stem for path in template_paths()}

        assert shipped - script - definitions == set()
