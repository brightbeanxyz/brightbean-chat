"""The rule trigger's panel in L4-A's drawer (SPEC §10, issue #22).

The drawer is ``apps/flows``'s and the binding is this app's, so the seam between
them — a POST from that form becoming a ``config_json`` the matcher understands —
is what these cover. The panel itself renders the CRM's §11.4 filter bar from
``contacts/_filter_bar.html``, so what has to be asserted is the round trip:
posted, validated against the trigger schema, stored, and read back by the panel.
"""

import json

import pytest

from apps.campaigns.tests.support import contact_for
from apps.contacts import services as contact_services
from apps.flows.models import Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow


def url(tenancy, flow, suffix=""):
    return f"/w/{tenancy.workspace.id}/flows/{flow.pk}/triggers/{suffix}"


def _flow(workspace):
    return published_flow(workspace, graph([node("a", "action", {"actions": [{"verb": "add_tag", "tag": "hit"}]})]))


def triggers(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])


@pytest.mark.django_db
class TestCreatingARuleTrigger:
    def test_the_event_and_both_id_filters_round_trip(self, tenancy, client_for):
        flow = _flow(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        response = client_for(tenancy.owner).post(
            url(tenancy, flow, "create/"),
            {"type": TriggerType.RULE, "event": "tag_added", "tag_id": str(tag.pk), "filter": ""},
        )

        assert triggers(response)["triggersChanged"] is True
        assert Trigger.objects.for_workspace(tenancy.workspace).get().config_json == {
            "event": "tag_added",
            "tag_id": str(tag.pk),
        }

    def test_an_id_filter_belonging_to_another_event_is_dropped(self, tenancy, client_for):
        """A stale tag_id on a field_changed rule would be a saved setting the
        panel no longer shows and the matcher would still honour."""
        flow = _flow(tenancy.workspace)
        tag, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")

        client_for(tenancy.owner).post(
            url(tenancy, flow, "create/"),
            {"type": TriggerType.RULE, "event": "field_changed", "tag_id": str(tag.pk)},
        )

        assert Trigger.objects.for_workspace(tenancy.workspace).get().config_json == {"event": "field_changed"}

    def test_a_condition_filter_is_stored_and_fires_against_it(self, tenancy, client_for):
        """The whole seam: the filter bar's hidden input becomes `filters`, the
        trigger schema accepts it, and the matcher evaluates it at fire time."""
        flow = _flow(tenancy.workspace)
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        lead, _ = contact_services.get_or_create_tag(tenancy.workspace, "Lead")
        document = {"match": "all", "rules": [{"source": "tag", "key": str(vip.pk), "op": "has"}]}

        client_for(tenancy.owner).post(
            url(tenancy, flow, "create/"),
            {"type": TriggerType.RULE, "event": "tag_added", "filter": json.dumps(document)},
        )

        assert Trigger.objects.for_workspace(tenancy.workspace).get().config_json["filters"] == document

        from apps.flows.models import FlowExecution

        plain = contact_for(tenancy.workspace, first_name="Plain")
        contact_services.add_tag(plain, lead)
        assert not FlowExecution.objects.for_workspace(tenancy.workspace).exists()

        vip_contact = contact_for(tenancy.workspace, first_name="Vip")
        contact_services.add_tag(vip_contact, vip)
        assert FlowExecution.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_malformed_filter_is_refused_with_a_2xx_toast(self, tenancy, client_for):
        flow = _flow(tenancy.workspace)

        response = client_for(tenancy.owner).post(
            url(tenancy, flow, "create/"),
            {"type": TriggerType.RULE, "event": "tag_added", "filter": "{not json"},
        )

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"
        assert "triggersChanged" not in triggers(response)
        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()

    def test_a_filter_naming_another_workspace_s_tag_is_refused(self, tenancy, other_tenancy, client_for):
        """The id is in the body, so tests/idor.py cannot reach it — the engine's
        scoped resolution is what refuses it, as "unknown" rather than
        "forbidden"."""
        flow = _flow(tenancy.workspace)
        theirs, _ = contact_services.get_or_create_tag(other_tenancy.workspace, "Theirs")
        document = {"match": "all", "rules": [{"source": "tag", "key": str(theirs.pk), "op": "has"}]}

        response = client_for(tenancy.owner).post(
            url(tenancy, flow, "create/"),
            {"type": TriggerType.RULE, "event": "tag_added", "filter": json.dumps(document)},
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()


@pytest.mark.django_db
class TestThePanel:
    def test_it_hydrates_the_filter_bar_from_the_stored_document(self, tenancy, client_for):
        flow = _flow(tenancy.workspace)
        vip, _ = contact_services.get_or_create_tag(tenancy.workspace, "VIP")
        document = {"match": "all", "rules": [{"source": "tag", "key": str(vip.pk), "op": "has"}]}
        trigger = Trigger(flow=flow, type=TriggerType.RULE, config_json={"event": "tag_added", "filters": document})
        trigger.save()

        config = (
            client_for(tenancy.owner).get(url(tenancy, flow, f"form/?trigger={trigger.pk}")).context["filter_config"]
        )

        assert config["document"] == document
        assert [row["label"] for row in config["tags"]] == ["VIP"]

    def test_another_type_s_panel_pays_nothing_for_it(self, tenancy, client_for):
        """Four queries for a control the keyword panel never draws."""
        flow = _flow(tenancy.workspace)

        response = client_for(tenancy.owner).get(url(tenancy, flow, "form/?type=keyword"))

        assert response.context["filter_config"] is None
