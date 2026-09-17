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


def _as_form(mapping: dict[str, Any]) -> dict[str, Any]:
    """A mapping dict flattened into the review form's field names.

    ``<kind>|<requirement key>|<field>``, which is what ``_mapping_from`` reads
    back. Going through the form rather than writing ``record.mapping`` directly
    is the point of the end-to-end test: it exercises the parser a browser hits.
    """
    fields: dict[str, Any] = {}
    for kind, answers in mapping.items():
        for key, answer in (answers or {}).items():
            for field, value in (answer or {}).items():
                if value is not None:
                    fields[f"{kind}|{key}|{field}"] = value
    return fields


def _record_for(tenancy: Any) -> FlowImport:
    record = FlowImport.objects.for_workspace(tenancy.workspace).first()
    assert record is not None
    return record


def _real_library() -> Any:
    """The shipped template directory, resolved before a test moves BASE_DIR."""
    from django.conf import settings

    from apps.flows.portability.library import LIBRARY_RELATIVE_PATH

    return settings.BASE_DIR / LIBRARY_RELATIVE_PATH


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

    def test_a_second_confirmation_under_a_lock_imports_nothing_further(self, tenancy: Any, client_for: Any) -> None:
        """``confirm_import`` takes the row's lock and answers None the second time.

        The status check in the view is a cheap early exit, not the guard: two
        requests can both read ``pending`` before either writes. The guard is
        the lock, and this drives it directly rather than through two clients,
        because a real race is not something a test can schedule reliably.
        """
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)

        first = portability.confirm_import(record, user=tenancy.owner)
        second = portability.confirm_import(record, user=tenancy.owner)

        assert first is not None and len(first) == 1
        assert second is None
        assert Flow.objects.for_workspace(tenancy.workspace).filter(name="Tiny").count() == 1

    def test_the_status_transition_commits_with_the_flows(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        _upload(client, tenancy, _tiny())
        record = _record_for(tenancy)

        portability.confirm_import(record, user=tenancy.owner)

        record.refresh_from_db()
        assert record.status == FlowImportStatus.APPLIED
        assert record.applied_at is not None

    def test_a_trigger_the_rewrite_breaks_refuses_the_whole_import(self, tenancy: Any, client_for: Any) -> None:
        """A kept trigger that cannot be created is a refusal, not a quiet drop.

        The person asked to keep it — the mapping step let them skip it — so
        importing the flow while discarding what starts it would be the worst of
        the three outcomes.
        """
        from apps.flows.models import Trigger

        document = _tiny()
        document["flows"][0]["triggers"] = [
            {"type": "ref_url", "platform": None, "config": {"ref": "welcome", "link_handle": ""}}
        ]
        client = client_for(tenancy.owner)
        _upload(client, tenancy, document)
        record = _record_for(tenancy)
        # Past the plan's own length check, so the failure lands where the
        # rewrite produces it rather than where the form is read.
        requirement = next(r for r in portability.requirements_for(record.document) if r.kind == "link_handle")
        record.mapping = {"link_handle": {requirement.key: {"value": "x" * 101}}}
        record.save(update_fields=["mapping"])
        before = Flow.objects.for_workspace(tenancy.workspace).count()

        response = client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))

        assert response.status_code == 204
        assert b"Not ready to import" in response["HX-Trigger"].encode()
        assert Flow.objects.for_workspace(tenancy.workspace).count() == before
        assert not Trigger.objects.for_workspace(tenancy.workspace).filter(type="ref_url").exists()

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


class TestTheTemplateGallery:
    """Step zero: the shipped library, reachable without downloading a file."""

    def test_it_lists_every_shipped_template(self, tenancy: Any, client_for: Any) -> None:
        from django.utils.html import escape

        from apps.flows.portability.library import template_cards

        response = client_for(tenancy.owner).get(_url("template_gallery", tenancy))
        assert response.status_code == 200
        body = response.content.decode()
        cards = template_cards()
        assert cards, "the repository ships no templates"
        for card in cards:
            # ``escape`` because a name is author text and reaches the page
            # escaped, which is the property the XSS test below asserts directly.
            assert escape(card.name) in body

    def test_it_warns_when_a_channel_is_not_connected(self, tenancy: Any, client_for: Any) -> None:
        """The whole reason the badge is on the card: the review page asks this
        as a blocking question, which is one click too late to be a warning."""
        response = client_for(tenancy.owner).get(_url("template_gallery", tenancy))

        assert b"Needs Instagram" in response.content

    def test_the_warning_goes_away_once_the_platform_is_connected(self, tenancy: Any, client_for: Any) -> None:
        from apps.flows.tests.support import connection_for

        connection_for(tenancy.workspace, platform="instagram", external_id="ig-1")

        response = client_for(tenancy.owner).get(_url("template_gallery", tenancy))

        assert b"Needs Instagram" not in response.content
        assert b"Instagram" in response.content

    def test_a_connection_that_cannot_send_does_not_count_as_connected(self, tenancy: Any, client_for: Any) -> None:
        """ "Connected" means active. A disabled or re-auth-pending connection
        cannot deliver a message, so a card that stopped warning about it would
        be telling somebody the template is ready to run when it is not."""
        from apps.channels.models import ConnectionStatus
        from apps.flows.tests.support import connection_for

        connection = connection_for(tenancy.workspace, platform="instagram", external_id="ig-1")
        connection.status = ConnectionStatus.DISABLED
        connection.save(update_fields=["status"])

        response = client_for(tenancy.owner).get(_url("template_gallery", tenancy))

        assert b"Needs Instagram" in response.content

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_is_refused(self, tenancy: Any, client_for: Any, role: str) -> None:
        assert client_for(tenancy.user_for(role)).get(_url("template_gallery", tenancy)).status_code == 403

    def test_another_tenants_workspace_is_not_found(self, tenancy: Any, other_tenancy: Any, client_for: Any) -> None:
        url = _url("template_gallery", tenancy)
        assert client_for(other_tenancy.owner).get(url).status_code == 404

    def test_it_survives_an_empty_library(self, tenancy: Any, client_for: Any, settings: Any, tmp_path: Any) -> None:
        """A directory that is missing or empty is an empty state, not a 500.

        Asserted on the context rather than on the empty state's wording: the
        page renders and has nothing to offer is the behaviour, and the sentence
        that says so is copy a restyle is free to rewrite.
        """
        settings.BASE_DIR = tmp_path
        response = client_for(tenancy.owner).get(_url("template_gallery", tenancy))
        assert response.status_code == 200
        assert response.context["cards"] == []
        assert response.context["categories"] == []

    def test_template_text_reaches_the_page_escaped(
        self, tenancy: Any, client_for: Any, settings: Any, tmp_path: Any
    ) -> None:
        """A shipped file is still author text, held to the same rule as an upload."""
        library = tmp_path / "flow-templates"
        library.mkdir()
        document = json.loads((_real_library() / "telegram-welcome-and-faq.json").read_text())
        document["flows"][0]["name"] = '<script>alert("xss")</script>'
        (library / "nasty.json").write_text(json.dumps(document))
        settings.BASE_DIR = tmp_path

        body = client_for(tenancy.owner).get(_url("template_gallery", tenancy)).content.decode()
        assert "&lt;script&gt;" in body
        assert "<script>alert" not in body


class TestStartingFromATemplate:
    def test_it_creates_only_the_import_row(self, tenancy: Any, client_for: Any) -> None:
        """The same promise the upload keeps: nothing but the FlowImport row."""
        response = client_for(tenancy.owner).post(
            _url("template_start", tenancy, template_slug="telegram-welcome-and-faq")
        )

        assert response.status_code == 302
        assert not Flow.objects.for_workspace(tenancy.workspace).exists()
        record = _record_for(tenancy)
        assert record.status == FlowImportStatus.PENDING
        assert record.original_filename == "telegram-welcome-and-faq.json"
        assert response["Location"] == _url("import_review", tenancy, flow_import_id=record.pk)

    def test_the_whole_wizard_runs_from_a_template(self, tenancy: Any, client_for: Any) -> None:
        """Template to live draft, through the review form a browser actually
        posts rather than by assigning ``record.mapping``. That is what makes
        this different from the confirm test below: it exercises
        ``_mapping_from``'s parser on the way through.
        """
        from apps.flows.models import Trigger
        from apps.flows.tests.portability_support import answer_channels

        client = client_for(tenancy.owner)
        client.post(_url("template_start", tenancy, template_slug="telegram-welcome-and-faq"))
        record = _record_for(tenancy)

        answered = answer_channels(record.document, record.mapping)
        review = _url("import_review", tenancy, flow_import_id=record.pk)
        assert client.post(review, _as_form(answered)).status_code == 302

        assert client.post(_url("import_confirm", tenancy, flow_import_id=record.pk)).status_code == 204
        flows = Flow.objects.for_workspace(tenancy.workspace)
        assert flows.exists()
        assert all(flow.status == "draft" for flow in flows)
        assert not Trigger.objects.for_workspace(tenancy.workspace).filter(enabled=True).exists()

    def test_starting_in_one_workspace_creates_nothing_in_another(
        self, tenancy: Any, other_tenancy: Any, client_for: Any
    ) -> None:
        """A shipped template is the same file in every workspace, so the usual
        "another tenant's object id" shape does not exist here. What has to hold
        instead is that the row lands scoped to the caller and is invisible next
        door."""
        client_for(tenancy.owner).post(_url("template_start", tenancy, template_slug="telegram-welcome-and-faq"))

        assert FlowImport.objects.for_workspace(tenancy.workspace).count() == 1
        assert FlowImport.objects.for_workspace(other_tenancy.workspace).count() == 0
        assert Flow.objects.for_workspace(other_tenancy.workspace).count() == 0

    def test_the_mapping_arrives_prefilled_exactly_as_an_uploads_does(self, tenancy: Any, client_for: Any) -> None:
        """The point of ``_begin_import`` being one function.

        Mirrors ``test_each_one_imports_into_an_empty_workspace``: a clean
        workspace should have nothing left to answer but its channels.
        """
        client_for(tenancy.owner).post(_url("template_start", tenancy, template_slug="sms-keyword-opt-in"))
        record = _record_for(tenancy)
        plan = portability.plan_import(tenancy.workspace, record.document, record.mapping)
        assert [answer.requirement.kind for answer in plan.unanswered] == ["platform"]

    def test_the_review_page_renders_for_a_template_import(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        client.post(_url("template_start", tenancy, template_slug="telegram-welcome-and-faq"))
        response = client.get(_url("import_review", tenancy, flow_import_id=_record_for(tenancy).pk))
        assert response.status_code == 200
        assert "Telegram welcome and FAQ" in response.content.decode()

    def test_confirming_creates_the_flow_as_a_draft_with_triggers_off(self, tenancy: Any, client_for: Any) -> None:
        from apps.flows.models import Trigger
        from apps.flows.tests.portability_support import answer_channels

        client = client_for(tenancy.owner)
        client.post(_url("template_start", tenancy, template_slug="telegram-welcome-and-faq"))
        record = _record_for(tenancy)
        record.mapping = answer_channels(record.document, record.mapping)
        record.save(update_fields=["mapping"])

        # 204 with a toast + ``flowsChanged`` event: the confirm answers HTMX,
        # the same as it does for an uploaded import.
        response = client.post(_url("import_confirm", tenancy, flow_import_id=record.pk))
        assert response.status_code == 204

        flows = Flow.objects.for_workspace(tenancy.workspace)
        assert flows.exists()
        assert all(flow.status == "draft" for flow in flows)
        assert not Trigger.objects.for_workspace(tenancy.workspace).filter(enabled=True).exists()

    def test_trigger_types_reach_the_page_as_labels_not_enum_values(self, tenancy: Any, client_for: Any) -> None:
        """The gallery names a trigger the way the trigger panel does.

        ``default_reply`` and ``story_reply`` are storage values; a card showing
        one is the same feature speaking with two voices.
        """
        from apps.flows.triggers.registry import spec_for

        body = client_for(tenancy.owner).get(_url("template_gallery", tenancy)).content.decode()

        # Read off the registry rather than spelled out, so a copy pass on a
        # label changes one place. What is pinned is that the *storage value*
        # never reaches the page, which is the bug this guards.
        for trigger_type in ("default_reply", "story_reply"):
            spec = spec_for(trigger_type)
            assert spec is not None, trigger_type
            assert spec.label in body, trigger_type
            assert trigger_type not in body

    def test_the_error_page_keeps_the_category_filters(
        self, tenancy: Any, client_for: Any, settings: Any, tmp_path: Any
    ) -> None:
        """The 400 renders the same page, so it gets the same context.

        It used to be built from a second copy of the dict that passed no
        categories, which silently dropped the filter chips.
        """
        library = tmp_path / "flow-templates"
        library.mkdir()
        (library / "broken.json").write_text("{ not json")
        (library / "fine.json").write_bytes((_real_library() / "sms-keyword-opt-in.json").read_bytes())
        settings.BASE_DIR = tmp_path

        response = client_for(tenancy.owner).post(_url("template_start", tenancy, template_slug="broken"))
        assert response.status_code == 400
        assert response.context["categories"] == ["Starters"]
        assert [entry["card"].name for entry in response.context["cards"]] == ["SMS keyword opt-in"]
        assert response.context["errors"]

    @pytest.mark.parametrize(
        "slug",
        [
            "no-such-template",
            "..",
            "../conftest",
            "..%2F..%2Fconftest",
            "telegram-welcome-and-faq.json",
            "/etc/passwd",
        ],
    )
    def test_an_unknown_or_traversing_slug_is_a_404_and_stores_nothing(
        self, tenancy: Any, client_for: Any, slug: str
    ) -> None:
        """Most of these never reach the view — Django's ``<slug:…>`` converter
        does not match them, so the URL does not resolve. The ones that do reach
        it are refused by the whitelist. Either way the answer is 404 and the
        table is untouched."""
        response = client_for(tenancy.owner).post(f"/w/{tenancy.workspace.pk}/flows/templates/{slug}/start/")
        assert response.status_code == 404
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    def test_a_get_cannot_start_an_import(self, tenancy: Any, client_for: Any) -> None:
        response = client_for(tenancy.owner).get(
            _url("template_start", tenancy, template_slug="telegram-welcome-and-faq")
        )
        assert response.status_code == 405
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_role_without_edit_flows_is_refused(self, tenancy: Any, client_for: Any, role: str) -> None:
        response = client_for(tenancy.user_for(role)).post(
            _url("template_start", tenancy, template_slug="telegram-welcome-and-faq")
        )
        assert response.status_code == 403
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()

    def test_a_template_that_does_not_validate_stores_nothing(
        self, tenancy: Any, client_for: Any, settings: Any, tmp_path: Any
    ) -> None:
        library = tmp_path / "flow-templates"
        library.mkdir()
        (library / "broken.json").write_text("{ not json")
        settings.BASE_DIR = tmp_path

        response = client_for(tenancy.owner).post(_url("template_start", tenancy, template_slug="broken"))
        assert response.status_code == 400
        assert not FlowImport.objects.for_workspace(tenancy.workspace).exists()
