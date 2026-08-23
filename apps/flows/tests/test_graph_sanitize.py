"""Markup stored in a graph is normalised on the way in.

The threat this closes is member-to-member, not contact-to-member. ``html_body``
is the one config field that stores markup, the flow builder's body editor
renders it into a ``contentEditable`` with ``innerHTML``, and the deployment's
CSP grants ``'unsafe-eval'`` for Alpine — so markup one workspace member stores
is markup that executes in another member's browser when they open the flow.

``edit_flows`` is an Editor permission; ``manage_channels``, ``manage_members``,
``manage_workspace_settings`` and ``manage_api_keys`` are admin-only
(``apps.members.roles``). Without this, an Editor could store a payload and wait
for an Admin to open the node.

The editor sanitizes too, but the editor is not the only writer: the flows API
takes a whole ``graph_json`` document over ``PUT``, so the authoritative pass has
to be on the write path every client shares.
"""

from typing import Any

import pytest

from apps.flows.models import FlowVersion
from apps.flows.schema.nodes import node_spec
from apps.flows.schema.sanitize import sanitize_graph
from apps.flows.services import create_flow, save_draft
from apps.flows.tests.support import graph, node

pytestmark = pytest.mark.django_db

HOSTILE = "<p>Hi</p><div x-data x-init=\"fetch('/steal')\"></div>"


def email_graph(html_body: str) -> dict[str, Any]:
    return graph([node("n1", "send_email", {"subject": "Hello", "html_body": html_body})])


class TestTheDeclarationDrivesIt:
    def test_send_email_declares_its_html_field(self) -> None:
        """A node that stores markup says so, rather than being special-cased."""
        spec = node_spec("send_email")
        assert spec is not None
        assert spec.html_fields == ("html_body",)

    def test_no_other_node_stores_markup(self) -> None:
        """If this ever grows, the new entry needs the same scrutiny."""
        from apps.flows.schema import NODE_TYPES

        with_html = {name for name in NODE_TYPES if (node_spec(name) or spec_none()).html_fields}
        assert with_html == {"send_email"}


def spec_none() -> Any:
    class _Empty:
        html_fields: tuple[str, ...] = ()

    return _Empty()


class TestSanitizeGraph:
    def test_alpine_directives_are_stripped(self) -> None:
        cleaned = sanitize_graph(email_graph(HOSTILE))
        body = cleaned["nodes"][0]["config"]["html_body"]
        assert "x-init" not in body
        assert "x-data" not in body
        assert "<p>Hi</p>" in body

    @pytest.mark.parametrize(
        "payload",
        [
            "<script>steal()</script>",
            '<p onclick="steal()">x</p>',
            '<a href="javascript:steal()">x</a>',
            '<iframe src="https://evil.test"></iframe>',
            '<p @click="steal()">x</p>',
        ],
    )
    def test_every_execution_vector_is_removed(self, payload: str) -> None:
        body = sanitize_graph(email_graph(payload))["nodes"][0]["config"]["html_body"]
        for marker in ("script", "onclick", "javascript:", "iframe", "@click"):
            assert marker not in body

    def test_legitimate_markup_survives(self) -> None:
        kept = '<p>Hello <strong>there</strong></p><p><a href="https://x.test">link</a></p>'
        body = sanitize_graph(email_graph(kept))["nodes"][0]["config"]["html_body"]

        assert "<strong>there</strong>" in body
        assert 'href="https://x.test"' in body
        # The allowlist adds `rel` to every link rather than preserving the
        # author's, so this is a normalisation rather than a byte round trip.
        assert 'rel="noopener noreferrer"' in body

    def test_it_is_idempotent(self) -> None:
        """A save that does not touch the body must not churn it."""
        once = sanitize_graph(email_graph(HOSTILE))
        assert sanitize_graph(once) == once

    def test_a_clean_graph_is_returned_unchanged(self) -> None:
        clean = email_graph("<p>Hello</p>")
        assert sanitize_graph(clean) is clean

    def test_other_node_types_are_untouched(self) -> None:
        """Only declared fields are markup; everything else is text."""
        document = graph([node("n1", "note", {"text": "<b>not markup</b>"})])
        assert sanitize_graph(document) is document

    @pytest.mark.parametrize(
        "document",
        [None, "", 7, [], {}, {"nodes": "nope"}, {"nodes": [None, 7, "x"]}, {"nodes": [{"type": "send_email"}]}],
    )
    def test_a_malformed_graph_does_not_raise(self, document: Any) -> None:
        """This runs before schema validation, on whatever arrived."""
        sanitize_graph(document)


class TestTheWritePath:
    def test_save_draft_normalises_what_it_stores(self, tenancy: Any) -> None:
        """The API's PUT lands here, so this is the pass that cannot be bypassed."""
        flow = create_flow(workspace=tenancy.workspace, name="Campaign")

        save_draft(flow, email_graph(HOSTILE), user=tenancy.owner)

        stored = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow).graph_json
        body = stored["nodes"][0]["config"]["html_body"]
        assert "x-init" not in body
        assert "<p>Hi</p>" in body

    def test_the_api_cannot_smuggle_markup_past_the_editor(self, tenancy: Any, client_for: Any) -> None:
        """An Editor holds `edit_flows` and can PUT a document by hand."""
        import json

        flow = create_flow(workspace=tenancy.workspace, name="Campaign")
        editor = client_for(tenancy.user_for("editor"))

        response = editor.put(
            f"/w/{tenancy.workspace.pk}/api/flows/{flow.pk}/",
            data=json.dumps({"graph": email_graph(HOSTILE)}),
            content_type="application/json",
        )

        assert response.status_code in {200, 204}
        stored = FlowVersion.objects.for_workspace(tenancy.workspace).get(flow=flow).graph_json
        assert "x-init" not in stored["nodes"][0]["config"]["html_body"]
