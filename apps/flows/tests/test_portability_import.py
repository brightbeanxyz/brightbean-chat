"""An import is untrusted input, and this is where that is proved.

Three properties, each of which the issue names:

* **Malicious or malformed JSON is rejected safely** — size caps, schema
  validation, no object creation before the confirm.
* **Nothing reaches the ORM before the confirm.** Asserted by counting rows
  across an upload and a review, not by reading the code.
* **An imported flow cannot reach a template engine.** SECURITY-BASELINE §3
  bans SSTI, and a shared template is exactly the document that would try.
"""

import json
from typing import Any

import pytest

from apps.flows import portability
from apps.flows.models import Flow, FlowVersion, Trigger
from apps.flows.portability.envelope import MAX_DOCUMENT_BYTES, MAX_DOCUMENT_DEPTH
from apps.flows.schema import MAX_EDGES, MAX_NODES
from apps.flows.tests.portability_support import seed
from apps.flows.tests.support import graph, node
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db


def _document(tenancy: Any) -> dict[str, Any]:
    return portability.export_document(seed(tenancy).flow)


def _minimal() -> dict[str, Any]:
    """The smallest document this format accepts, built by hand rather than exported."""
    return {
        "app": "brightbean-chat",
        "format": 1,
        "schema": 1,
        "entry": "flow-1",
        "flows": [
            {
                "key": "flow-1",
                "name": "Hello",
                "folder": "",
                "graph": graph([node("n1", "send_message", {"blocks": [{"type": "text", "text": "hi"}]})]),
                "triggers": [],
            }
        ],
        "requirements": {kind: [] for kind in portability.REQUIREMENT_KINDS},
    }


def _rejected(payload: Any) -> list[str]:
    raw = payload if isinstance(payload, bytes | str) else json.dumps(payload)
    document, issues = portability.parse_and_validate(raw)
    assert document is None, "this document should have been refused"
    return [issue.message for issue in issues]


class TestMalformedInputIsRefused:
    def test_a_document_over_the_size_cap_is_not_parsed(self) -> None:
        oversized = b'{"padding": "' + b"x" * (MAX_DOCUMENT_BYTES + 10) + b'"}'
        assert "limit" in " ".join(_rejected(oversized))

    def test_an_empty_file_is_refused(self) -> None:
        assert _rejected(b"")

    def test_something_that_is_not_json_is_refused(self) -> None:
        assert "not valid JSON" in " ".join(_rejected(b"{ this is not json"))

    def test_a_json_array_is_refused(self) -> None:
        assert _rejected(b"[1, 2, 3]")

    def test_a_document_nested_past_the_depth_cap_is_refused(self) -> None:
        payload = "x"
        for _ in range(MAX_DOCUMENT_DEPTH + 5):
            payload = f"[{payload}]"
        assert "nests deeper" in " ".join(_rejected(f'{{"flows": {payload.replace("x", "1")}}}'))

    def test_a_null_character_is_refused(self) -> None:
        """JSON allows ``\\u0000``; Postgres ``jsonb`` does not.

        Without this the document would reach ``FlowImport.document`` and fail at
        execute time — a 500 for a value a stranger supplied.
        """
        document = _minimal()
        document["flows"][0]["name"] = "Hello" + chr(0) + "there"
        assert "null character" in " ".join(_rejected(document))

    def test_a_null_character_in_a_key_is_refused(self) -> None:
        document = _minimal()
        document["requirements"]["tag"] = [{"key": chr(0)}]
        assert "null character" in " ".join(_rejected(document))

    def test_an_unknown_envelope_key_is_refused(self) -> None:
        """SECURITY-BASELINE §7's mass-assignment guard, at the envelope."""
        document = _minimal()
        document["published"] = True
        assert "not a recognised key" in " ".join(_rejected(document))

    def test_a_document_from_another_app_is_refused(self) -> None:
        document = _minimal()
        document["app"] = "some-other-product"
        assert _rejected(document)

    def test_an_unknown_format_version_is_refused_rather_than_guessed_at(self) -> None:
        document = _minimal()
        document["format"] = 99
        assert _rejected(document)

    def test_an_unknown_graph_schema_version_is_refused(self) -> None:
        document = _minimal()
        document["schema"] = 99
        assert _rejected(document)

    def test_an_entry_naming_no_flow_is_refused(self) -> None:
        document = _minimal()
        document["entry"] = "flow-9"
        assert "not a flow in this file" in " ".join(_rejected(document))

    def test_two_flows_sharing_a_key_are_refused(self) -> None:
        document = _minimal()
        document["flows"].append(dict(document["flows"][0]))
        assert "share the key" in " ".join(_rejected(document))


class TestTheGraphLimitsApply:
    def test_more_nodes_than_the_cap_are_refused(self) -> None:
        document = _minimal()
        document["flows"][0]["graph"] = graph(
            [node(f"n{index}", "note", {"text": "."}) for index in range(MAX_NODES + 1)]
        )
        assert "the limit is" in " ".join(_rejected(document))

    def test_more_edges_than_the_cap_are_refused(self) -> None:
        document = _minimal()
        edges = [
            {"id": f"e{index}", "source": "n1", "sourceHandle": "default", "target": "n1"}
            for index in range(MAX_EDGES + 1)
        ]
        document["flows"][0]["graph"] = graph([node("n1", "note", {"text": "."})], edges)
        assert "the limit is" in " ".join(_rejected(document))

    def test_an_unknown_node_type_is_refused(self) -> None:
        document = _minimal()
        document["flows"][0]["graph"] = graph([node("n1", "run_shell_command", {})])
        assert "not a known node type" in " ".join(_rejected(document))

    def test_an_unknown_node_config_key_is_refused(self) -> None:
        document = _minimal()
        document["flows"][0]["graph"] = graph(
            [node("n1", "send_message", {"blocks": [{"type": "text", "text": "hi"}], "internal": True})]
        )
        assert "not a recognised key" in " ".join(_rejected(document))

    def test_an_invalid_trigger_config_is_refused(self) -> None:
        document = _minimal()
        document["flows"][0]["triggers"] = [{"type": "keyword", "platform": None, "config": {"keywords": "help"}}]
        assert _rejected(document)

    def test_an_unknown_trigger_type_is_refused(self) -> None:
        document = _minimal()
        document["flows"][0]["triggers"] = [{"type": "shell", "platform": None, "config": {}}]
        assert "not a trigger type" in " ".join(_rejected(document))

    def test_a_half_wired_graph_imports_as_a_draft(self, tenancy: Any) -> None:
        """A dangling edge is an ordinary draft, not grounds for refusing a file.

        SPEC §16 has the builder autosaving every two seconds, so a graph caught
        mid-edit is expected. Refusing to import one would be stricter than the
        builder that produced it — it surfaces as a note and blocks publish.
        """
        document = _minimal()
        document["flows"][0]["graph"] = graph(
            [node("n1", "send_message", {"blocks": [{"type": "text", "text": "hi"}]})],
            [{"id": "e1", "source": "n1", "sourceHandle": "default", "target": "gone"}],
        )
        parsed, issues = portability.parse_and_validate(json.dumps(document))
        assert not issues
        assert parsed is not None
        plan = portability.plan_import(tenancy.workspace, parsed, {})
        assert plan.can_apply
        assert any("cannot be published" in note for note in plan.notes)


class TestNothingIsCreatedBeforeTheConfirm:
    def test_validating_and_planning_write_nothing(self, tenancy: Any) -> None:
        from apps.campaigns.models import Sequence
        from apps.contacts.models import CustomField, Tag

        raw = portability.serialize(_document(tenancy))
        clean = create_tenancy("dry-run")

        before = (
            Flow.objects.for_workspace(clean.workspace).count(),
            Tag.objects.for_workspace(clean.workspace).count(),
            CustomField.objects.for_workspace(clean.workspace).count(),
            Sequence.objects.for_workspace(clean.workspace).count(),
            Trigger.objects.for_workspace(clean.workspace).count(),
        )

        document, issues = portability.parse_and_validate(raw)
        assert not issues
        assert document is not None
        mapping = portability.default_mapping(clean.workspace, document, user=clean.owner)
        portability.plan_import(clean.workspace, document, mapping)
        portability.requirements_for(document)
        portability.outbound_requests(document)

        after = (
            Flow.objects.for_workspace(clean.workspace).count(),
            Tag.objects.for_workspace(clean.workspace).count(),
            CustomField.objects.for_workspace(clean.workspace).count(),
            Sequence.objects.for_workspace(clean.workspace).count(),
            Trigger.objects.for_workspace(clean.workspace).count(),
        )
        assert before == after

    def test_an_unanswered_requirement_refuses_the_whole_import(self, tenancy: Any) -> None:
        raw = portability.serialize(_document(tenancy))
        clean = create_tenancy("unanswered")
        document, _ = portability.parse_and_validate(raw)
        assert document is not None

        with pytest.raises(portability.ImportNotReadyError):
            portability.apply_import(clean.workspace, document, {}, user=clean.owner)

        assert not Flow.objects.for_workspace(clean.workspace).exists()

    def test_a_segment_cannot_be_conjured(self, tenancy: Any) -> None:
        """The one kind the mapping step refuses to invent.

        A segment is a saved filter. Creating an empty one to satisfy a
        reference would silently change what the imported condition matches,
        which is worse than saying no.
        """
        raw = portability.serialize(_document(tenancy))
        clean = create_tenancy("no-segments")
        document, _ = portability.parse_and_validate(raw)
        assert document is not None

        segment = next(r for r in portability.requirements_for(document) if r.kind == "segment")
        assert not segment.creatable
        plan = portability.plan_import(
            clean.workspace, document, {"segment": {segment.key: {"action": portability.ACTION_CREATE}}}
        )
        problem = next(r for r in plan.resolutions if r.requirement.kind == "segment")
        assert "cannot be created from a template" in problem.problem


class TestUntrustedContent:
    def test_template_syntax_in_an_imported_body_stays_literal_text(self, tenancy: Any) -> None:
        """SECURITY-BASELINE §3: no engine ever evaluates a message body.

        The renderer is plain token substitution against a fixed grammar, so a
        stranger's ``{% … %}`` or ``{{ x.__class__ }}`` is inert — not because
        it is escaped, but because there is no template engine in the path at
        all.
        """
        from apps.flows.rendering import RenderContext, render

        hostile = "{% load os %}{{ settings.SECRET_KEY }}{{ x.__class__.__mro__ }}${7*7}#{7*7}"
        document = _minimal()
        document["flows"][0]["graph"] = graph(
            [node("n1", "send_message", {"blocks": [{"type": "text", "text": hostile}]})]
        )
        parsed, issues = portability.parse_and_validate(json.dumps(document))
        assert not issues
        assert parsed is not None

        clean = create_tenancy("ssti")
        flows = portability.apply_import(clean.workspace, parsed, {}, user=clean.owner)
        stored = FlowVersion.objects.for_workspace(clean.workspace).filter(flow=flows[0]).first()
        assert stored is not None
        body = stored.graph_json["nodes"][0]["config"]["blocks"][0]["text"]
        assert body == hostile

        # And rendering it evaluates nothing. ``{% … %}``, ``${…}`` and ``#{…}``
        # are not the placeholder grammar, so they survive as literal text;
        # ``{{ settings.SECRET_KEY }}`` *is* the grammar's shape, so it is looked
        # up as a token — finds nothing, because there is no attribute access and
        # no namespace called ``settings`` — and renders as the empty string.
        # Either way, no engine ever sees any of it.
        from django.conf import settings

        rendered = render(body, RenderContext(system={"first_name": "Ada"}))
        assert rendered == "{% load os %}${7*7}#{7*7}"
        assert str(settings.SECRET_KEY) not in rendered
        assert "49" not in rendered

    def test_imported_markup_goes_through_the_html_allowlist(self, tenancy: Any) -> None:
        """``save_draft`` sanitizes declared HTML fields, and the import uses it.

        A stranger's ``html_body`` would otherwise be markup the builder writes
        into another member's browser with ``innerHTML``.
        """
        document = _minimal()
        document["flows"][0]["graph"] = graph(
            [
                node(
                    "n1",
                    "send_email",
                    {
                        "subject": "Hi",
                        "html_body": '<p onclick="steal()">Hi</p><script>alert(1)</script>',
                    },
                )
            ]
        )
        parsed, issues = portability.parse_and_validate(json.dumps(document))
        assert not issues
        assert parsed is not None

        clean = create_tenancy("markup")
        flows = portability.apply_import(clean.workspace, parsed, {}, user=clean.owner)
        stored = FlowVersion.objects.for_workspace(clean.workspace).filter(flow=flows[0]).first()
        assert stored is not None
        body = stored.graph_json["nodes"][0]["config"]["html_body"]
        assert "<script>" not in body
        assert "onclick" not in body

    def test_a_lying_manifest_cannot_hide_a_reference(self, tenancy: Any) -> None:
        """Requirements are re-derived from the flows, never read out of the file.

        Emptying the manifest does change the *shape* of the questions — without
        a label, a tag reached by id can no longer be folded onto the one
        reached by name, so it gets a question of its own. What it cannot do is
        make a reference disappear: every synthetic reference in the document
        still has exactly one requirement pointing at it, which is the property
        that stops a hand-edited file from importing a dangling id.
        """
        raw = portability.serialize(_document(tenancy))
        document, _ = portability.parse_and_validate(raw)
        assert document is not None

        honest = {r.ref for r in portability.requirements_for(document) if r.ref}
        assert honest, "the fixture flow addresses several objects by id"

        document["requirements"] = {kind: [] for kind in portability.REQUIREMENT_KINDS}
        stripped = portability.requirements_for(document)
        assert {r.ref for r in stripped if r.ref} == honest
        # Less tidy, never wrong: more questions, not fewer.
        assert len(stripped) >= len(honest)

    def test_a_manifest_entry_nothing_references_is_ignored_and_reported(self, tenancy: Any) -> None:
        document = _minimal()
        document["requirements"]["tag"] = [{"key": "invented", "name": "Invented", "used_by": []}]
        parsed, issues = portability.parse_and_validate(json.dumps(document))
        assert not issues
        assert parsed is not None

        assert not portability.requirements_for(parsed)
        plan = portability.plan_import(tenancy.workspace, parsed, {})
        assert any("nothing in it actually references" in note for note in plan.notes)

    def test_an_external_request_url_is_surfaced_before_the_import_can_run(self, tenancy: Any) -> None:
        """The importer did not choose this address, so they are shown it."""
        raw = portability.serialize(_document(tenancy))
        document, _ = portability.parse_and_validate(raw)
        assert document is not None

        calls = portability.outbound_requests(document)
        assert [(call["method"], call["url"]) for call in calls] == [("POST", "https://api.example.com/leads")]
        assert portability.plan_import(tenancy.workspace, document, {}).outbound_requests == calls
