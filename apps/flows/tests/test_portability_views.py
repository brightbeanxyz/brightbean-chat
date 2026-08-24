"""The export download and the three-step import wizard, through HTTP.

What matters here is the gate and the order, not the markup: who may call each
route, that a bad upload writes nothing, that the confirm is the only thing that
creates a flow, and that a stranger's text reaches the page escaped.
"""

import json
from typing import Any

import pytest
from django.urls import reverse

from apps.flows import portability
from apps.flows.models import Flow, FlowImport, FlowImportStatus
from apps.flows.tests.portability_support import seed
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def _url(name: str, tenancy: Any, **kwargs: Any) -> str:
    return reverse(f"flows:{name}", kwargs={"workspace_id": tenancy.workspace.pk, **kwargs})


def _upload(client: Any, tenancy: Any, payload: Any, *, filename: str = "template.flow.json") -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile

    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post(
        _url("import_start", tenancy),
        {"file": SimpleUploadedFile(filename, raw, content_type="application/json")},
    )


def _record_for(tenancy: Any) -> FlowImport:
    record = FlowImport.objects.for_workspace(tenancy.workspace).first()
    assert record is not None
    return record


class TestExport:
    def test_an_editor_downloads_the_flow(self, tenancy: Any, client_for: Any) -> None:
        seeded = seed(tenancy)
        response = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).get(
            _url("export", tenancy, flow_id=seeded.flow.pk)
        )

        assert response.status_code == 200
        assert response["Content-Type"] == "application/json"
        assert response["Content-Disposition"] == 'attachment; filename="welcome.flow.json"'
        document = json.loads(response.content)
        assert document["app"] == "brightbean-chat"
        assert document["flows"][0]["name"] == "Welcome"

    def test_the_bundle_route_carries_the_closure(self, tenancy: Any, client_for: Any) -> None:
        seeded = seed(tenancy)
        response = client_for(tenancy.owner).get(_url("export_bundle", tenancy, flow_id=seeded.flow.pk))
        assert response.status_code == 200
        assert len(json.loads(response.content)["flows"]) == 2
        assert "-bundle.flow.json" in response["Content-Disposition"]

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_is_refused(self, tenancy: Any, client_for: Any, role: str) -> None:
        """403, not 404: they are in this workspace and merely lack the permission,
        which tells them nothing they did not already know (CONTRIBUTING.md)."""
        seeded = seed(tenancy)
        response = client_for(tenancy.user_for(role)).get(_url("export", tenancy, flow_id=seeded.flow.pk))
        assert response.status_code == 403

    def test_another_tenants_flow_is_not_found(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        seeded = seed(tenancy)
        url = reverse(
            "flows:export",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_id": seeded.flow.pk},
        )
        assert client_for(other_tenancy.owner).get(url).status_code == 404


class TestUpload:
    def test_the_page_renders(self, tenancy: Any, client_for: Any) -> None:
        response = client_for(tenancy.owner).get(_url("import_start", tenancy))
        assert response.status_code == 200

    def test_a_valid_file_creates_only_the_import_row(self, tenancy: Any, client_for: Any) -> None:
        document = portability.export_document(seed(tenancy).flow)
        before = Flow.objects.for_workspace(tenancy.workspace).count()

        response = _upload(client_for(tenancy.owner), tenancy, document)

        assert response.status_code == 302
        assert Flow.objects.for_workspace(tenancy.workspace).count() == before
        record = _record_for(tenancy)
        assert record.status == FlowImportStatus.PENDING
        assert record.original_filename == "template.flow.json"

    def test_a_malformed_file_is_refused_and_stores_nothing(self, tenancy: Any, client_for: Any) -> None:
        response = _upload(client_for(tenancy.owner), tenancy, b"{ not json")

        assert response.status_code == 400
        assert b"not valid JSON" in response.content
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    def test_no_file_is_refused(self, tenancy: Any, client_for: Any) -> None:
        response = client_for(tenancy.owner).post(_url("import_start", tenancy), {})
        assert response.status_code == 400
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    def test_an_oversized_file_is_refused_before_it_is_parsed(self, tenancy: Any, client_for: Any) -> None:
        oversized = b'{"padding":"' + b"x" * (portability.MAX_DOCUMENT_BYTES + 10) + b'"}'
        response = _upload(client_for(tenancy.owner), tenancy, oversized)
        assert response.status_code == 400
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_cannot_upload(self, tenancy: Any, client_for: Any, role: str) -> None:
        assert _upload(client_for(tenancy.user_for(role)), tenancy, _tiny()).status_code == 403


def _tiny() -> dict[str, Any]:
    from apps.flows.tests.support import graph, node

    return {
        "app": "brightbean-chat",
        "format": 1,
        "schema": 1,
        "entry": "flow-1",
        "flows": [
            {
                "key": "flow-1",
                "name": "Tiny",
                "folder": "",
                "graph": graph([node("n1", "send_message", {"blocks": [{"type": "text", "text": "hi"}]})]),
                "triggers": [],
            }
        ],
        "requirements": {kind: [] for kind in portability.REQUIREMENT_KINDS},
    }


def _two_triggers() -> dict[str, Any]:
    """``_tiny()`` with two unbound keyword triggers.

    Unbound on purpose: a bound trigger would raise a channel requirement, and
    this fixture exists for the tests that are about triggers rather than about
    the mapping step.
    """
    document = _tiny()
    document["flows"][0]["triggers"] = [
        {"type": "keyword", "platform": None, "config": {"keywords": [{"text": "first", "mode": "exact"}]}},
        {"type": "keyword", "platform": None, "config": {"keywords": [{"text": "second", "mode": "exact"}]}},
    ]
    return document


class TestReviewAndConfirm:
    def test_the_review_page_lists_the_outbound_calls(self, tenancy: Any, client_for: Any) -> None:
        document = portability.export_document(seed(tenancy).flow)
        client = client_for(tenancy.owner)
        _upload(client, tenancy, document)

        response = client.get(_url("import_review", tenancy, flow_import_id=_record_for(tenancy).pk))

        assert response.status_code == 200
        assert b"https://api.example.com/leads" in response.content
        assert b"calls out to the internet" in response.content

    def test_imported_text_reaches_the_page_escaped(self, tenancy: Any, client_for: Any) -> None:
        """A template's flow name is a stranger's text like any other."""
        document = _tiny()
        document["flows"][0]["name"] = '<script>alert("xss")</script>'
        client = client_for(tenancy.owner)
        _upload(client, tenancy, document)

        response = client.get(_url("import_review", tenancy, flow_import_id=_record_for(tenancy).pk))

        assert b"<script>alert" not in response.content
        assert b"&lt;script&gt;" in response.content

    def test_posting_the_form_saves_answers_without_creating_anything(self, tenancy: Any, client_for: Any) -> None:
        from apps.contacts.models import Tag

        document = portability.export_document(seed(tenancy).flow)
        other = tenancy.workspace
        client = client_for(tenancy.owner)
        _upload(client, tenancy, document)
        record = _record_for(tenancy)
        before = Tag.objects.for_workspace(other).count()

        requirement = next(r for r in portability.requirements_for(record.document) if r.kind == "tag")
        response = client.post(
            _url("import_review", tenancy, flow_import_id=record.pk),
            {f"tag|{requirement.key}|action": "create", f"tag|{requirement.key}|name": "Imported VIP"},
        )

        # Post-redirect-get: the answers are stored and the dry run is re-read
        # by a GET, so a refresh is not a re-submission.
        assert response.status_code == 302
        record.refresh_from_db()
        assert record.mapping["tag"][requirement.key] == {"action": "create", "name": "Imported VIP"}
        assert Tag.objects.for_workspace(other).count() == before

    def test_a_form_field_naming_no_requirement_is_dropped(self, tenancy: Any, client_for: Any) -> None:
        """Mass-assignment guard: the form cannot introduce a requirement."""
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)

        client.post(
            _url("import_review", tenancy, flow_import_id=record.pk),
            {"tag|invented|action": "create", "tag|invented|name": "Nope"},
        )

        record.refresh_from_db()
        assert record.mapping == {}

    def test_a_trigger_can_be_skipped(self, tenancy: Any, client_for: Any) -> None:
        """The issue's "skip/keep triggers". Kept by default; skipped on request.

        Skipping is not the same as importing one disabled — every imported
        trigger is disabled anyway, so "skip" means the row is never created.

        Built on ``_two_triggers()`` rather than the full fixture flow: this is
        about the keep/skip answer, and a document that also raises a media
        requirement would make the confirm refuse for an unrelated reason.
        """
        from apps.flows.models import Trigger

        client = client_for(tenancy.owner)
        _upload(client, tenancy, _two_triggers())
        record = _record_for(tenancy)
        choices = portability.trigger_choices(record.document)
        assert [choice.keep for choice in choices] == [True, True]

        client.post(
            _url("import_review", tenancy, flow_import_id=record.pk),
            {
                f"trigger|{choices[0].key}|action": "skip",
                f"trigger|{choices[1].key}|action": "keep",
            },
        )

        record.refresh_from_db()
        assert not portability.trigger_choices(record.document, record.mapping)[0].keep
        before = Trigger.objects.for_workspace(tenancy.workspace).count()

        assert client.post(_url("import_confirm", tenancy, flow_import_id=record.pk)).status_code == 204

        landed = Trigger.objects.for_workspace(tenancy.workspace).filter(type="keyword")
        assert Trigger.objects.for_workspace(tenancy.workspace).count() - before == 1
        assert [trigger.config_json["keywords"][0]["text"] for trigger in landed] == ["second"]

    def test_triggers_are_kept_by_default(self, tenancy: Any, client_for: Any) -> None:
        from apps.flows.models import Trigger

        client = client_for(tenancy.owner)
        _upload(client, tenancy, _two_triggers())
        record = _record_for(tenancy)
        before = Trigger.objects.for_workspace(tenancy.workspace).count()

        client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        assert Trigger.objects.for_workspace(tenancy.workspace).count() - before == 2

    def test_confirm_creates_the_flows_and_marks_the_import(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)
        before = Flow.objects.for_workspace(tenancy.workspace).count()

        response = client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        assert response.status_code == 204
        assert response.content == b""
        assert Flow.objects.for_workspace(tenancy.workspace).count() == before + 1
        record.refresh_from_db()
        assert record.status == FlowImportStatus.APPLIED
        assert record.applied_at is not None

    def test_confirm_twice_creates_one_set_of_flows(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)
        client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))
        count = Flow.objects.for_workspace(tenancy.workspace).count()

        client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        assert Flow.objects.for_workspace(tenancy.workspace).count() == count

    def test_confirm_refuses_while_a_requirement_is_unanswered(self, tenancy: Any, client_for: Any) -> None:
        document = portability.export_document(seed(tenancy).flow)
        client = client_for(tenancy.owner)
        _upload(client, tenancy, document)
        record = _record_for(tenancy)
        record.mapping = {}
        record.save(update_fields=["mapping"])
        before = Flow.objects.for_workspace(tenancy.workspace).count()

        response = client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        assert response.status_code == 204
        assert b"Not ready to import" in response["HX-Trigger"].encode()
        assert Flow.objects.for_workspace(tenancy.workspace).count() == before
        record.refresh_from_db()
        assert record.status == FlowImportStatus.PENDING

    def test_discard_removes_a_pending_import(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)

        assert client.post(_url("import_discard", tenancy, flow_import_id=record.pk)).status_code == 204
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    def test_discard_keeps_an_applied_import_as_the_record(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)
        client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        client.post(_url("import_discard", tenancy, flow_import_id=record.pk))

        assert FlowImport.objects.for_workspace(tenancy.workspace).filter(pk=record.pk).exists()

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_cannot_review_or_confirm(self, tenancy: Any, client_for: Any, role: str) -> None:
        _upload(client_for(tenancy.owner), tenancy, _tiny())
        record = _record_for(tenancy)
        client = client_for(tenancy.user_for(role))

        assert client.get(_url("import_review", tenancy, flow_import_id=record.pk)).status_code == 403
        assert client.post(_url("import_confirm", tenancy, flow_import_id=record.pk)).status_code == 403

    def test_another_tenants_import_is_not_found(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        _upload(client_for(tenancy.owner), tenancy, _tiny())
        record = _record_for(tenancy)
        url = reverse(
            "flows:import_review",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_import_id": record.pk},
        )
        assert client_for(other_tenancy.owner).get(url).status_code == 404


class TestHousekeeping:
    def test_unconfirmed_imports_are_swept_and_applied_ones_are_not(self, tenancy: Any) -> None:
        from datetime import timedelta

        from django.utils import timezone

        from apps.flows.housekeeping import IMPORTS_KEPT_FOR, discard_stale_imports

        old = FlowImport(workspace=tenancy.workspace, document=_tiny())
        old.save()
        applied = FlowImport(workspace=tenancy.workspace, document=_tiny(), status=FlowImportStatus.APPLIED)
        applied.save()
        stale = timezone.now() - IMPORTS_KEPT_FOR - timedelta(hours=1)
        FlowImport.objects.for_workspace(tenancy.workspace).update(created_at=stale)

        discard_stale_imports()

        remaining = set(FlowImport.objects.for_workspace(tenancy.workspace).values_list("pk", flat=True))
        assert remaining == {applied.pk}

    def test_a_fresh_import_survives_the_sweep(self, tenancy: Any) -> None:
        from apps.flows.housekeeping import discard_stale_imports

        record = FlowImport(workspace=tenancy.workspace, document=_tiny())
        record.save()

        discard_stale_imports()

        assert FlowImport.objects.for_workspace(tenancy.workspace).filter(pk=record.pk).exists()
