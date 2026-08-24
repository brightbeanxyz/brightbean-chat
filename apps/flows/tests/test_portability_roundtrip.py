"""SPEC §21 phase 3: "flow export/import round-trips including triggers".

The criterion is asserted as **byte equality**, not as a bespoke "semantically
identical" comparison. Export a flow, import the file into a workspace that has
none of what it needs, export the result, and diff the two files. Two design
choices are what make that possible, and each is worth the assertion it buys:

* the serialisation is canonical (sorted keys, no timestamps), and
* synthetic references are numbered in **walk order** rather than derived from
  names, so the second export mints the same references whatever the importing
  workspace decided to call things.

A normalised comparison would let a real regression hide inside whatever the
normaliser chose to ignore. This one cannot: if the round trip changes anything
at all, the diff says exactly what.
"""

from typing import Any

import pytest

from apps.flows import portability
from apps.flows.models import Flow, FlowStatus, FlowVersion, Trigger
from apps.flows.tests.portability_support import answer_channels, seed, seed_second_flow
from tests.support import create_tenancy

pytestmark = pytest.mark.django_db


def _answer_everything(workspace: Any, document: dict[str, Any], *, user: Any) -> dict[str, Any]:
    """A mapping that creates what it can and picks what already exists.

    This is what the wizard's own defaults produce for a workspace that has
    nothing (``default_mapping``), plus an answer for the two kinds no default
    can guess. A **segment** cannot be created from a template, so one is made
    here and mapped; a **media asset** cannot either, so an asset of the same
    filename is uploaded and mapped.

    Answering like for like is what the byte-equality claim is *about*: the
    round trip preserves the flow, it does not pretend a URL and a library
    asset are the same choice. ``TestMediaWithoutALibrary`` covers the other
    answer, where they deliberately differ.
    """
    mapping = portability.default_mapping(workspace, document, user=user)
    for requirement in portability.requirements_for(document):
        answer = (mapping.get(requirement.kind) or {}).get(requirement.key) or {}
        if requirement.kind == "platform":
            # A connection of the right platform, so a bound trigger lands bound.
            # There is no default here on purpose — leaving a channel blank
            # widens the trigger to every platform its type supports — so the
            # like-for-like answer has to be given explicitly.
            answer_channels(document, mapping, connections=True, workspace=workspace)
            continue
        if answer.get("action"):
            continue
        if requirement.kind == "segment":
            from apps.contacts.services import create_segment

            segment = create_segment(
                workspace, name=requirement.name or "Imported", filter_json={"match": "all", "rules": []}
            )
            answer = {"action": portability.ACTION_MAP, "id": str(segment.pk)}
        elif requirement.kind == "media":
            from apps.media_library.models import MediaAsset

            asset = MediaAsset.objects.create(
                workspace=workspace,
                filename=requirement.name or "asset.png",
                kind=requirement.detail or "image",
                mime="image/png",
                size=11,
                file=f"media/{requirement.name or 'asset.png'}",
            )
            answer = {"action": portability.ACTION_MAP, "id": str(asset.pk)}
        else:  # pragma: no cover - every other kind has a default
            answer = {"action": portability.ACTION_BLANK}
        mapping.setdefault(requirement.kind, {})[requirement.key] = answer
    return mapping


def _import_into(tenancy: Any, raw: str) -> list[Flow]:
    document, issues = portability.parse_and_validate(raw)
    assert not issues, [issue.message for issue in issues]
    assert document is not None
    mapping = _answer_everything(tenancy.workspace, document, user=tenancy.owner)
    plan = portability.plan_import(tenancy.workspace, document, mapping)
    assert plan.can_apply, [resolution.problem for resolution in plan.unanswered]
    return portability.apply_import(tenancy.workspace, document, mapping, user=tenancy.owner)


class TestRoundTrip:
    def test_a_flow_with_every_reference_kind_round_trips_byte_for_byte(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        first = portability.serialize(portability.export_document(seeded.flow))

        clean = create_tenancy("importer")
        imported = _import_into(clean, first)

        second = portability.serialize(portability.export_document(imported[0]))
        assert second == first

    def test_the_round_trip_carries_the_triggers(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        first = portability.serialize(portability.export_document(seeded.flow))

        clean = create_tenancy("importer-triggers")
        imported = _import_into(clean, first)

        landed = Trigger.objects.for_workspace(clean.workspace).filter(flow=imported[0]).order_by("priority")
        assert [trigger.type for trigger in landed] == [trigger.type for trigger in seeded.triggers]
        # The whole point of the byte comparison: the trigger configs survive
        # the translation, not merely the trigger types.
        assert portability.serialize(portability.export_document(imported[0])) == first

    def test_a_start_flow_bundle_round_trips_byte_for_byte(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        first = portability.serialize(portability.export_document(seeded.flow, bundle=True))

        clean = create_tenancy("importer-bundle")
        imported = _import_into(clean, first)

        assert len(imported) == 2  # the entry flow and start_flow's target
        assert portability.serialize(portability.export_document(imported[0], bundle=True)) == first

    def test_a_bundle_carries_the_flows_a_sequence_step_starts(self, tenancy: Any) -> None:
        """The closure follows sequence steps out; it cannot follow them back in.

        A sequence is a workspace object with a schedule and live enrollments,
        so the export format carries it as a **requirement** to create or map,
        not as a definition to recreate (see ``docs/flow-templates.md``). The
        bundle therefore contains the flows the source's steps started — without
        them the template is incomplete — while the imported sequence starts out
        empty, waiting for somebody to put those flows on its rungs.

        So: three flows out, three flows in, and a second bundle export of the
        *imported* entry flow is legitimately smaller. That asymmetry is stated
        rather than asserted away, because a normalising comparison that hid it
        would also hide a real regression in the closure.
        """
        seeded = seed(tenancy)
        seed_second_flow(seeded)
        raw = portability.serialize(portability.export_document(seeded.flow, bundle=True))
        document, issues = portability.parse_and_validate(raw)
        assert not issues
        assert document is not None
        assert [flow["name"] for flow in document["flows"]] == ["Welcome", "Follow up", "Day two"]

        clean = create_tenancy("importer-sequence-bundle")
        imported = _import_into(clean, raw)
        assert [flow.name for flow in imported] == ["Welcome", "Follow up", "Day two"]

        again = portability.parse_and_validate(
            portability.serialize(portability.export_document(imported[0], bundle=True))
        )[0]
        assert again is not None
        assert [flow["name"] for flow in again["flows"]] == ["Welcome", "Follow up"]

    def test_a_second_export_of_the_same_flow_is_identical(self, tenancy: Any) -> None:
        """Determinism, on its own, so a round-trip failure is never ambiguous."""
        seeded = seed(tenancy)
        assert portability.serialize(portability.export_document(seeded.flow)) == portability.serialize(
            portability.export_document(seeded.flow)
        )


class TestWhatArrives:
    def test_an_imported_flow_is_an_unpublished_draft(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = portability.serialize(portability.export_document(seeded.flow))

        clean = create_tenancy("importer-draft")
        imported = _import_into(clean, raw)

        flow = imported[0]
        assert flow.status == FlowStatus.DRAFT
        versions = FlowVersion.objects.for_workspace(clean.workspace).filter(flow=flow)
        assert [(version.version, version.published) for version in versions] == [(1, False)]

    def test_imported_triggers_are_disabled(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = portability.serialize(portability.export_document(seeded.flow))

        clean = create_tenancy("importer-disabled")
        imported = _import_into(clean, raw)

        landed = Trigger.objects.for_workspace(clean.workspace).filter(flow=imported[0])
        assert landed.exists()
        assert not any(trigger.enabled for trigger in landed)

    def test_references_land_on_the_importing_workspace_objects(self, tenancy: Any) -> None:
        """No synthetic reference survives into a stored graph."""
        from apps.contacts.models import Tag

        seeded = seed(tenancy)
        raw = portability.serialize(portability.export_document(seeded.flow))

        clean = create_tenancy("importer-refs")
        imported = _import_into(clean, raw)

        stored = FlowVersion.objects.for_workspace(clean.workspace).filter(flow=imported[0]).first()
        assert stored is not None
        serialised = str(stored.graph_json)
        document, _ = portability.parse_and_validate(raw)
        assert document is not None
        for requirement in portability.requirements_for(document):
            if requirement.ref:
                assert requirement.ref not in serialised, f"{requirement.kind} reference was left dangling"

        # And the tag the condition matched is the importing workspace's own.
        tag = Tag.objects.for_workspace(clean.workspace).filter(name__iexact="VIP").first()
        assert tag is not None
        assert str(tag.pk) in serialised
        assert str(seeded.tag.pk) not in serialised


class TestMediaWithoutALibrary:
    """The escape hatch: a template with a picture, a workspace with no assets.

    Answering a media requirement with a URL rewrites the block from a library
    id to a URL — SPEC §11.1 takes either — so the flow arrives complete rather
    than pointing at an asset that is not there. The round trip is deliberately
    *not* byte-identical here, because the answer changed what the flow holds.
    """

    def test_a_url_answer_replaces_the_library_id(self, tenancy: Any) -> None:
        seeded = seed(tenancy)
        raw = portability.serialize(portability.export_document(seeded.flow))
        document, issues = portability.parse_and_validate(raw)
        assert not issues
        assert document is not None

        clean = create_tenancy("importer-media-url")
        mapping = answer_channels(document, portability.default_mapping(clean.workspace, document, user=clean.owner))
        for requirement in portability.requirements_for(document):
            if requirement.kind == "media":
                mapping.setdefault("media", {})[requirement.key] = {
                    "action": portability.ACTION_MAP,
                    "url": "https://cdn.example.com/banner.png",
                }
            elif requirement.kind == "segment":
                from apps.contacts.services import create_segment

                segment = create_segment(clean.workspace, name="Engaged", filter_json={"match": "all", "rules": []})
                mapping.setdefault("segment", {})[requirement.key] = {
                    "action": portability.ACTION_MAP,
                    "id": str(segment.pk),
                }
        imported = portability.apply_import(clean.workspace, document, mapping, user=clean.owner)

        version = FlowVersion.objects.for_workspace(clean.workspace).filter(flow=imported[0]).first()
        assert version is not None
        blocks = version.graph_json["nodes"][0]["config"]["blocks"]
        image = next(block for block in blocks if block["type"] == "image")
        assert image["url"] == "https://cdn.example.com/banner.png"
        assert "media_id" not in image
        # The card carries both forms in one field, so the URL simply lands there.
        card = next(block for block in blocks if block["type"] == "card")
        assert card["image"] == "https://cdn.example.com/banner.png"
