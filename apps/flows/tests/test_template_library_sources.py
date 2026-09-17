"""Every shipped template is what its definition generates.

``flow-templates/*.json`` is a **build artifact**. The readable, reviewable form
is :mod:`apps.flows.tests.template_sources`; the JSON is what the real exporter
makes of it. This module is the staleness gate between the two, the same
arrangement ``static/flows/flow-schema.json`` has with
``export_flow_schema --check`` and ``apps/flows/tests/test_export.py``.

It also regenerates. With ``BRIGHTBEAN_REGENERATE_TEMPLATES=1`` set, each test
writes its file instead of asserting against it::

    BRIGHTBEAN_REGENERATE_TEMPLATES=1 pytest apps/flows/tests/test_template_library_sources.py

A test that can write is a wart. The alternative is a sixth management command
that needs a database and a throwaway workspace — one that, shipped, would write
rows into a self-hoster's database and files into a source tree they do not have.
The wart is the smaller of the two.
"""

import os

import pytest

from apps.flows.portability import export_document, serialize
from apps.flows.portability.library import library_path
from apps.flows.tests.template_sources import DEFINITIONS, TemplateSource, build
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db

#: Set to write the files rather than check them. Deliberately an environment
#: variable and not a pytest flag: it has to be impossible to trip by running
#: the suite the normal way, and CI never sets it.
REGENERATE = os.environ.get("BRIGHTBEAN_REGENERATE_TEMPLATES") == "1"

FILENAMES = tuple(source.filename for source in DEFINITIONS)


def _generated(source: TemplateSource) -> str:
    """``source`` through a real build and a real export, as canonical bytes."""
    tenancy = create_tenancy(slug=f"tpl-{source.filename[:20]}")
    return serialize(export_document(build(tenancy, source)))


class TestTheGeneratedTemplates:
    @pytest.mark.parametrize("filename", FILENAMES)
    def test_each_file_is_what_its_definition_generates(self, filename: str) -> None:
        source = next(item for item in DEFINITIONS if item.filename == filename)
        generated = _generated(source)
        path = library_path() / filename

        if REGENERATE:
            path.write_text(generated, encoding="utf-8")
            return

        assert path.exists(), f"{filename} has a definition but no file. Regenerate."
        assert path.read_text(encoding="utf-8") == generated, (
            f"{filename} is stale. Regenerate with BRIGHTBEAN_REGENERATE_TEMPLATES=1 pytest {__file__.split('/')[-1]}"
        )

    def test_no_definition_names_the_same_file_twice(self) -> None:
        assert len(set(FILENAMES)) == len(FILENAMES)
