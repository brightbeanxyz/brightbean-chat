"""The templates this repository ships, checked against the real importer.

Carries half of SPEC §21 phase 3's "flow export/import round-trips including
triggers" — the per-template half the issue asks for. The general claim lives in
``test_portability_roundtrip.py``; this module is that claim applied to each
shipped template, plus the rest of the acceptance criterion: a workspace missing
every requirement completes the mapping step, and publishing succeeds once the
connections are mapped.

``flow-templates/`` is the seed of the shared library and the directory a
community pull request adds to. A template that stops importing is therefore a
red build rather than a download that fails for a stranger, and the check is the
same call the upload path makes — not a weaker one against a different code
path.
"""

from typing import Any

import pytest

from apps.flows import portability
from apps.flows.models import Flow, Trigger
from apps.flows.portability.library import library_path, read_template, template_paths
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db

#: The three the issue asks for. Pinned by name so that deleting one is a
#: deliberate act with a test to update rather than a directory that quietly
#: empties.
EXPECTED = (
    "instagram-comment-to-dm-lead-magnet.json",
    "sms-keyword-opt-in.json",
    "telegram-welcome-and-faq.json",
)


def _paths() -> list[Any]:
    paths = template_paths()
    assert paths, f"no templates found in {library_path()}"
    return paths


class TestTheShippedTemplates:
    def test_the_starter_templates_are_all_there(self) -> None:
        assert tuple(path.name for path in _paths()) == EXPECTED

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_validates(self, name: str) -> None:
        path = library_path() / name
        document, issues = read_template(path)
        assert document is None or not issues
        assert document is not None, [f"{issue.path or '-'}: {issue.message}" for issue in issues]

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_imports_into_an_empty_workspace(self, name: str) -> None:
        """The acceptance criterion, per template: a workspace missing every
        requirement completes the mapping step with no dangling references."""
        document, issues = read_template(library_path() / name)
        assert not issues
        assert document is not None

        clean = create_tenancy(name.replace(".json", "")[:20])
        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)
        plan = portability.plan_import(clean.workspace, document, mapping)
        assert plan.can_apply, [
            f"{r.requirement.kind} {r.requirement.name or r.requirement.key}: {r.problem}" for r in plan.unanswered
        ]

        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)
        assert flows

        # No synthetic reference survived into a stored graph.
        landed = Flow.objects.for_workspace(clean.workspace).first()
        assert landed is not None
        stored = str([version.graph_json for version in landed.versions.all()])
        for requirement in portability.requirements_for(document):
            if requirement.ref:
                assert requirement.ref not in stored

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_round_trips(self, name: str) -> None:
        raw = (library_path() / name).read_text(encoding="utf-8")
        document, issues = read_template(library_path() / name)
        assert not issues
        assert document is not None

        clean = create_tenancy(f"rt-{name.replace('.json', '')}"[:20])
        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)
        # Like for like on the one thing no default can guess: the channel each
        # trigger watches. Without it a bound trigger lands unbound, which is a
        # legal import and a different document.
        for requirement in portability.requirements_for(document):
            if requirement.kind == "platform":
                from apps.flows.tests.support import connection_for

                connection = connection_for(
                    clean.workspace, platform=requirement.key, external_id=f"{requirement.key}-{clean.workspace.pk}"
                )
                mapping.setdefault("platform", {})[requirement.key] = {
                    "action": portability.ACTION_MAP,
                    "id": str(connection.pk),
                }
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)
        assert portability.serialize(portability.export_document(flows[0])) == raw

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_publishes_once_its_channel_is_mapped(self, name: str) -> None:
        """The rest of the acceptance criterion: "publish succeeds after
        connections mapped".

        Publishing is the strict gate — every graph error blocks it — so a
        template that imports and then cannot be published would be a template
        that does not work. Capability warnings do not block (SPEC §9.1), which
        is why mapping the channel matters for the warnings and not for the
        outcome.
        """
        from apps.flows.services import publish
        from apps.flows.tests.support import connection_for

        document, _ = read_template(library_path() / name)
        assert document is not None
        clean = create_tenancy(f"p-{name[:16]}".replace(".", "-"))

        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)
        for requirement in portability.requirements_for(document):
            if requirement.kind == "platform":
                connection = connection_for(
                    clean.workspace, platform=requirement.key, external_id=f"pub-{requirement.key}-{clean.workspace.pk}"
                )
                mapping.setdefault("platform", {})[requirement.key] = {
                    "action": portability.ACTION_MAP,
                    "id": str(connection.pk),
                }
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)

        for flow in flows:
            result = publish(flow, user=clean.owner)
            assert result.validation.is_publishable
            assert result.version.published

    @pytest.mark.parametrize("name", EXPECTED)
    def test_each_one_arrives_as_a_draft_with_triggers_off(self, name: str) -> None:
        document, _ = read_template(library_path() / name)
        assert document is not None
        clean = create_tenancy(f"d-{name.replace('.json', '')}"[:20])
        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)
        flows = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)

        for flow in flows:
            assert flow.status == "draft"
            assert not flow.versions.filter(published=True).exists()
        triggers = Trigger.objects.for_workspace(clean.workspace)
        assert triggers.exists()
        assert not triggers.filter(enabled=True).exists()

    @pytest.mark.parametrize("name", EXPECTED)
    def test_the_manifest_matches_what_the_flows_reference(self, name: str, tenancy: Any) -> None:
        """No stale entries, and nothing missing.

        The importer re-derives requirements from the flows and reports any
        manifest entry nothing references. A shipped template producing that
        note would mean the exporter and the importer disagree about what counts
        as a reference — which is exactly what one shared walk exists to prevent.
        """
        document, _ = read_template(library_path() / name)
        assert document is not None
        plan = portability.plan_import(tenancy.workspace, document, {})
        assert not [note for note in plan.notes if "manifest" in note]

    def test_no_template_carries_a_workspace_id(self) -> None:
        """A shipped template is a shared file like any other, and is held to the
        same rule: no ids, no credentials, no signed URLs."""
        for path in _paths():
            raw = path.read_text(encoding="utf-8")
            assert "/m/" not in raw, f"{path.name} carries a media delivery URL"
            document, _ = read_template(path)
            assert document is not None
            for entries in document["requirements"].values():
                for entry in entries:
                    assert "used_by" in entry


class TestTheValidateCommand:
    def test_it_passes_on_the_shipped_templates(self) -> None:
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("validate_flow_templates", stdout=out)
        assert "All 3 template(s) validate." in out.getvalue()
