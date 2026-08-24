"""What an export contains, and — more importantly — what it does not.

The issue's acceptance criterion is "exports contain zero workspace-identifying
data (assert on serialized output)", so that is what these assert on: the bytes
somebody would actually share, not an intermediate dict that might be scrubbed
later.

The line is drawn where ``docs/flow-templates.md`` draws it. An export carries
no database id, member identity, connection identity, credential, signed media
URL, workspace name or organization id. It does carry the author's own content —
the flow's name, its message text, an asset's filename — because that content is
the template.
"""

from typing import Any

import pytest

from apps.flows import portability
from apps.flows.portability import refs
from apps.flows.tests.portability_support import seed, seed_second_flow

pytestmark = pytest.mark.django_db


def _exported(flow: Any, *, bundle: bool = False) -> str:
    return portability.serialize(portability.export_document(flow, bundle=bundle))


class TestNothingIdentifyingLeaves:
    def test_no_workspace_object_id_appears_in_the_bytes(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = _exported(seeded.flow, bundle=True)

        forbidden = {
            "workspace": tenancy.workspace.pk,
            "organization": tenancy.organization.pk,
            "flow": seeded.flow.pk,
            "other flow": seeded.other_flow.pk,
            "tag": seeded.tag.pk,
            "custom field": seeded.field_row.pk,
            "sequence": seeded.sequence.pk,
            "segment": seeded.segment.pk,
            "media asset": seeded.media.pk,
            "channel connection": seeded.connection.pk,
            "member user": seeded.member.pk,
        }
        leaked = sorted(label for label, value in forbidden.items() if str(value) in raw)
        assert not leaked, f"the export carries this workspace's {', '.join(leaked)} id(s)"

    def test_no_member_identity_leaves(self, tenancy: Any) -> None:
        """A member reference is an ordinal. Not a name, not an email.

        Every other kind exports its name, because the importer cannot answer
        "which tag is this" without one. A person is the exception: their name
        and address identify them rather than describing a thing to map.
        """
        seeded = seed(tenancy)
        raw = _exported(seeded.flow)

        assert seeded.member.email not in raw
        assert (seeded.member.name or "\x00") not in raw
        members = portability.export_document(seeded.flow)["requirements"]["member"]
        assert members, "the flow assigns and notifies, so a member requirement must be raised"
        assert all("name" not in entry for entry in members)

    def test_no_workspace_or_organization_name_leaves(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = _exported(seeded.flow)
        assert tenancy.workspace.name not in raw
        assert tenancy.organization.name not in raw

    def test_request_header_values_are_blanked_and_reported(self, tenancy: Any) -> None:
        """A bearer token in a shared template is the exporter's credential."""
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        raw = portability.serialize(document)

        assert "deadbeef" not in raw
        node = next(n for n in document["flows"][0]["graph"]["nodes"] if n["type"] == "external_request")
        assert [header["value"] for header in node["config"]["headers"]] == ["", ""]
        # The names survive, because the name is what says what has to be supplied.
        assert [header["name"] for header in node["config"]["headers"]] == ["Authorization", "Content-Type"]
        assert {entry["name"] for entry in document["requirements"]["request_header"]} == {
            "Authorization",
            "Content-Type",
        }

    def test_the_sending_address_and_the_link_handle_are_stripped(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        raw = portability.serialize(document)

        assert "sales@acme.test" not in raw
        assert "acme_support_bot" not in raw
        mail = next(n for n in document["flows"][0]["graph"]["nodes"] if n["type"] == "send_email")
        assert mail["config"]["from_override"] == ""
        ref_url = next(t for t in document["flows"][0]["triggers"] if t["type"] == "ref_url")
        assert ref_url["config"]["link_handle"] == ""
        # The ref itself is the template's own content and travels.
        assert ref_url["config"]["ref"] == "welcome-2026"

    def test_a_media_delivery_url_is_never_exported(self, tenancy: Any) -> None:
        """A signed delivery URL is a capability, not a description of an asset."""
        from apps.media_library.delivery import delivery_url

        seeded = seed(tenancy)
        raw = _exported(seeded.flow)
        assert delivery_url(seeded.media) not in raw
        assert "/m/" not in raw

    def test_the_whatsapp_row_id_goes_and_the_reference_stays(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        template = document["flows"][0]["graph"]["nodes"][0]["config"]["whatsapp_template"]
        assert "template_id" not in template
        # <name>/<language> is the Cloud API's own key and what reaches the wire
        # (SPEC §6.5), so the node still works after the id is dropped.
        assert template["reference"] == "welcome_note/en_US"

    def test_a_connection_exports_as_its_platform(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        by_type = {trigger["type"]: trigger for trigger in document["flows"][0]["triggers"]}
        assert by_type["keyword"]["platform"] == "telegram"
        # SPEC §5: a null connection means "every connection of a matching
        # platform", so null is a value here and not an omission.
        assert by_type["rule"]["platform"] is None


class TestWhatItDoesCarry:
    def test_the_authors_own_content_travels(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = _exported(seeded.flow)
        assert "Welcome" in raw  # the flow's name
        assert "Starters" in raw  # its folder
        assert "Hello {{first_name}}, welcome." in raw  # a message body, placeholders intact
        assert "banner.png" in raw  # the filename hint for the media requirement

    def test_every_reference_kind_reaches_the_manifest(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        manifest = portability.export_document(seeded.flow)["requirements"]
        for kind in ("tag", "custom_field", "sequence", "segment", "member", "flow", "media", "platform"):
            assert manifest[kind], f"nothing was recorded for {kind}"
        assert set(manifest) == set(portability.REQUIREMENT_KINDS)

    def test_a_tag_named_and_matched_is_one_question(self, tenancy: Any) -> None:
        """``add_tag: "VIP"`` and a condition rule on the same tag's id are one tag."""
        seeded = seed(tenancy)
        tags = portability.export_document(seeded.flow)["requirements"]["tag"]
        assert len(tags) == 1
        assert tags[0]["name"] == "VIP"
        assert tags[0]["ref"], "the condition rule addresses it by id, so it needs a reference"

    def test_the_exported_graph_still_validates(self, tenancy: Any) -> None:
        """The property everything else leans on: a scrubbed graph is a valid graph.

        Synthetic references are UUIDs, and the fields that hold ids accept
        UUIDs — so an untrusted file can be checked against the unmodified graph
        schema before anything touches the ORM.
        """
        from apps.flows.schema import validate_graph

        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow, bundle=True)
        for flow in document["flows"]:
            result = validate_graph(flow["graph"])
            assert not result.document_errors, [issue.message for issue in result.document_errors]
            assert not result.graph_errors, [issue.message for issue in result.graph_errors]

    def test_every_reference_is_a_synthetic_uuid(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        for kind, entries in document["requirements"].items():
            for entry in entries:
                if "ref" in entry:
                    assert refs.is_uuid(entry["ref"]), f"{kind} reference is not a UUID"


class TestBundles:
    def test_a_bundle_follows_start_flow(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        names = [flow["name"] for flow in portability.export_document(seeded.flow, bundle=True)["flows"]]
        assert names == ["Welcome", "Follow up"]

    def test_a_bundle_follows_the_flows_a_sequence_step_starts(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        seed_second_flow(seeded)
        names = [flow["name"] for flow in portability.export_document(seeded.flow, bundle=True)["flows"]]
        assert names == ["Welcome", "Follow up", "Day two"]

    def test_a_single_flow_export_carries_one_flow(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        document = portability.export_document(seeded.flow)
        assert len(document["flows"]) == 1
        assert document["entry"] == document["flows"][0]["key"]

    def test_a_cycle_in_the_closure_terminates(self, tenancy: Any) -> None:
        """Two flows that start each other are a legal, and a very ordinary, graph."""
        from apps.flows.services import save_draft
        from apps.flows.tests.support import graph, node

        seeded = seed(tenancy)
        save_draft(seeded.other_flow, graph([node("back", "start_flow", {"flow_id": str(seeded.flow.pk)})]))
        names = [flow["name"] for flow in portability.export_document(seeded.flow, bundle=True)["flows"]]
        assert names == ["Welcome", "Follow up"]


class TestFilenames:
    def test_the_download_name_is_an_ascii_slug(self, tenancy: Any) -> None:
        """A flow name reaches a Content-Disposition header; a header is the wrong
        place to discover somebody put a newline in one."""
        from apps.flows.services import rename_flow

        seeded = seed(tenancy)
        rename_flow(seeded.flow, 'Welcome\r\n"; drop table — 🎉')
        name = portability.export_filename(seeded.flow)
        assert name == "welcome-drop-table.flow.json"
        assert portability.export_filename(seeded.flow, bundle=True).endswith("-bundle.flow.json")

    def test_a_flow_named_only_in_punctuation_still_gets_a_filename(self, tenancy: Any) -> None:
        from apps.flows.services import rename_flow

        seeded = seed(tenancy)
        rename_flow(seeded.flow, "!!!")
        assert portability.export_filename(seeded.flow) == "flow.flow.json"
