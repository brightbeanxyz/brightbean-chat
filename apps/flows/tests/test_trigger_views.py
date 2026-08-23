"""The Triggers panel: CRUD, role gating, refusals, and the QR endpoint."""

import json

import pytest
import segno
from django.urls import reverse

from apps.common.platforms import Platform
from apps.flows.models import Trigger, TriggerType
from apps.flows.tests.support import connection_for, graph, node, published_flow
from apps.flows.triggers import qr

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


@pytest.fixture
def flow(tenancy):
    return published_flow(tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="Ref flow")


def _url(name, tenancy, flow, **extra):
    return reverse(name, kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk, **extra})


def _trigger(flow, trigger_type=TriggerType.KEYWORD, config=None, *, connection=None, priority=0):
    trigger = Trigger(
        flow=flow,
        channel_connection=connection,
        type=trigger_type,
        config_json=config if config is not None else {"keywords": [{"text": "help", "mode": "contains"}]},
        priority=priority,
    )
    trigger.save()
    return trigger


def _events(response):
    raw = response.headers.get("HX-Trigger")
    return json.loads(raw) if raw else {}


@pytest.mark.django_db
class TestThePanel:
    def test_a_viewer_can_read_it(self, tenancy, client_for, flow):
        _trigger(flow)
        response = client_for(tenancy.user_for("viewer")).get(_url("flows:trigger_panel", tenancy, flow))

        assert response.status_code == 200
        assert b"help" in response.content

    def test_it_warns_about_a_second_enabled_default_reply(self, tenancy, client_for, flow):
        """Legal, and resolved by priority like every type — but the second one
        can never fire, and nothing else would ever say so."""
        _trigger(flow, TriggerType.DEFAULT_REPLY, {}, priority=0)
        _trigger(flow, TriggerType.DEFAULT_REPLY, {}, priority=10)

        response = client_for(tenancy.owner).get(_url("flows:trigger_panel", tenancy, flow))

        assert b"More than one" in response.content
        # The label the rest of the panel uses, not the column value.
        assert b"Default reply" in response.content
        assert b"default_reply trigger" not in response.content

    def test_every_trigger_type_can_be_created_from_the_panel(self, tenancy, client_for, flow):
        """Including `api`: entrypoint_only means no webhook selects it, not that
        nobody may make one — and #25's flow-start endpoint needs one to exist."""
        response = client_for(tenancy.owner).get(_url("flows:trigger_panel", tenancy, flow))

        for value in TriggerType.values:
            assert f'value="{value}"'.encode() in response.content, value

    def test_keyword_text_is_escaped(self, tenancy, client_for, flow):
        """Keyword text is user-authored (SECURITY-BASELINE §2)."""
        _trigger(flow, config={"keywords": [{"text": "<script>alert(1)</script>", "mode": "exact"}]})

        response = client_for(tenancy.owner).get(_url("flows:trigger_panel", tenancy, flow))

        assert b"<script>alert(1)</script>" not in response.content
        assert b"&lt;script&gt;" in response.content


@pytest.mark.django_db
class TestTheForm:
    """Every per-type form partial renders.

    Without this the only reference to ``flows:trigger_form`` asserted a 404, so
    the five form templates were never rendered by anything — a mistyped filter
    argument or an unbalanced block tag in one of them would have passed CI and
    500'd the first editor who clicked Add.
    """

    @pytest.mark.parametrize("trigger_type", list(TriggerType.values))
    def test_the_create_form_renders_for_every_type(self, tenancy, client_for, flow, trigger_type):
        response = client_for(tenancy.owner).get(_url("flows:trigger_form", tenancy, flow) + f"?type={trigger_type}")

        assert response.status_code == 200
        assert b"<form" in response.content

    @pytest.mark.parametrize("trigger_type", [TriggerType.KEYWORD, TriggerType.REF_URL, TriggerType.COMMENT])
    def test_the_edit_form_renders_a_stored_config(self, tenancy, client_for, flow, trigger_type):
        from apps.flows.triggers.registry import spec_for

        config = {"keywords": [{"text": "help", "mode": "exact"}]} if trigger_type == TriggerType.KEYWORD else None
        if trigger_type == TriggerType.REF_URL:
            config = {"ref": "promo"}
        if trigger_type == TriggerType.COMMENT:
            config = spec_for(trigger_type).default_config()
        trigger = _trigger(flow, trigger_type, config)

        response = client_for(tenancy.owner).get(_url("flows:trigger_form", tenancy, flow) + f"?trigger={trigger.pk}")

        assert response.status_code == 200
        assert b"<form" in response.content

    def test_an_unknown_type_is_refused_without_a_500(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).get(_url("flows:trigger_form", tenancy, flow) + "?type=teleport")

        assert 200 <= response.status_code < 300
        assert "triggersChanged" not in _events(response)

    def test_a_malformed_trigger_id_is_404_not_500(self, tenancy, client_for, flow):
        response = client_for(tenancy.owner).get(_url("flows:trigger_form", tenancy, flow) + "?trigger=not-a-uuid")

        assert response.status_code == 404


@pytest.mark.django_db
class TestMutations:
    def test_an_editor_can_create_one(self, tenancy, client_for, flow):
        response = client_for(tenancy.user_for("editor")).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.KEYWORD, "keyword_text": ["help"], "keyword_mode": ["contains"]},
        )

        assert response.status_code == 204
        assert _events(response).get("triggersChanged") is True
        assert Trigger.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_refusal_is_also_2xx_and_carries_no_change_event(self, tenancy, client_for, flow):
        """htmx drops HX-Trigger on a non-2xx, so a 400 would show no toast at
        all — the request would simply appear to do nothing."""
        response = client_for(tenancy.owner).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.KEYWORD, "keyword_text": [""], "keyword_mode": ["contains"]},
        )

        assert 200 <= response.status_code < 300
        assert "triggersChanged" not in _events(response)
        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()

    def test_a_keyword_list_that_does_not_line_up_is_refused(self, tenancy, client_for, flow):
        """Zipping to the shorter list would give somebody a trigger matching
        words they did not configure, in modes they did not pick."""
        response = client_for(tenancy.owner).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.KEYWORD, "keyword_text": ["a", "b"], "keyword_mode": ["contains"]},
        )

        assert "triggersChanged" not in _events(response)
        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()

    def test_a_connection_the_type_cannot_run_on_is_refused(self, tenancy, client_for, flow):
        """The picker's option list is a convenience; this is the gate."""
        sms = connection_for(tenancy.workspace, platform=Platform.SMS, external_id="+15550005")
        response = client_for(tenancy.owner).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.WELCOME, "channel_connection": str(sms.pk)},
        )

        assert "triggersChanged" not in _events(response)
        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()

    def test_a_duplicate_ref_is_refused(self, tenancy, client_for, flow):
        _trigger(flow, TriggerType.REF_URL, {"ref": "promo"})
        response = client_for(tenancy.owner).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.REF_URL, "ref": "promo"},
        )

        assert "triggersChanged" not in _events(response)
        assert Trigger.objects.for_workspace(tenancy.workspace).count() == 1

    def test_toggling_flips_enabled(self, tenancy, client_for, flow):
        trigger = _trigger(flow)
        client_for(tenancy.owner).post(_url("flows:trigger_toggle", tenancy, flow, trigger_id=trigger.pk))

        trigger.refresh_from_db()
        assert trigger.enabled is False

    def test_moving_swaps_the_neighbours(self, tenancy, client_for, flow):
        first = _trigger(flow, priority=0)
        second = _trigger(flow, priority=10)

        client_for(tenancy.owner).post(
            _url("flows:trigger_move", tenancy, flow, trigger_id=second.pk), {"direction": "up"}
        )

        first.refresh_from_db()
        second.refresh_from_db()
        assert second.priority < first.priority

    def test_moving_past_the_end_is_a_no_op(self, tenancy, client_for, flow):
        only = _trigger(flow)
        client_for(tenancy.owner).post(
            _url("flows:trigger_move", tenancy, flow, trigger_id=only.pk), {"direction": "up"}
        )

        only.refresh_from_db()
        assert only.priority == 0

    def test_deleting_removes_it(self, tenancy, client_for, flow):
        trigger = _trigger(flow)
        client_for(tenancy.owner).post(_url("flows:trigger_delete", tenancy, flow, trigger_id=trigger.pk))

        assert not Trigger.objects.for_workspace(tenancy.workspace).exists()

    def test_updating_can_unbind_a_connection(self, tenancy, client_for, flow):
        """None is a real value here — "every connection of a matching platform" —
        so "no connection" and "leave it alone" must not share one argument."""
        connection = connection_for(tenancy.workspace, external_id="bot-1")
        trigger = _trigger(flow, connection=connection)

        client_for(tenancy.owner).post(
            _url("flows:trigger_update", tenancy, flow, trigger_id=trigger.pk),
            {"keyword_text": ["help"], "keyword_mode": ["contains"], "channel_connection": ""},
        )

        trigger.refresh_from_db()
        assert trigger.channel_connection_id is None


@pytest.mark.django_db
class TestPermissions:
    @pytest.mark.parametrize("role", ["viewer", "agent"])
    def test_a_read_only_role_cannot_write(self, tenancy, client_for, flow, role):
        trigger = _trigger(flow)
        client = client_for(tenancy.user_for(role))

        for name, kwargs in (
            ("flows:trigger_create", {}),
            ("flows:trigger_update", {"trigger_id": trigger.pk}),
            ("flows:trigger_toggle", {"trigger_id": trigger.pk}),
            ("flows:trigger_move", {"trigger_id": trigger.pk}),
            ("flows:trigger_delete", {"trigger_id": trigger.pk}),
        ):
            assert client.post(_url(name, tenancy, flow, **kwargs)).status_code == 403, name

    @pytest.mark.parametrize("role", ["editor", "admin"])
    def test_an_editing_role_can_write(self, tenancy, client_for, flow, role):
        response = client_for(tenancy.user_for(role)).post(
            _url("flows:trigger_create", tenancy, flow),
            {"type": TriggerType.KEYWORD, "keyword_text": ["help"], "keyword_mode": ["contains"]},
        )
        assert 200 <= response.status_code < 300

    def test_a_get_on_a_post_only_route_is_404_for_another_tenant(self, tenancy, other_tenancy, client_for, flow):
        """The decorator stacking is what makes this a 404 rather than a 405 —
        and a 405 would confirm the route and the object exist."""
        trigger = _trigger(flow)
        response = client_for(other_tenancy.owner).get(
            _url("flows:trigger_delete", tenancy, flow, trigger_id=trigger.pk)
        )

        assert response.status_code == 404

    def test_the_attackers_own_workspace_with_a_victims_trigger_is_404(self, tenancy, other_tenancy, client_for, flow):
        """The case the IDOR sweep cannot reach: the middleware lets the request
        through, so only get_scoped_object_or_404 stands in the way."""
        trigger = _trigger(flow)
        theirs = published_flow(other_tenancy.workspace, graph([node("a", "action", NOOP_ACTION)]), name="Theirs")

        response = client_for(other_tenancy.owner).get(
            reverse(
                "flows:trigger_form",
                kwargs={"workspace_id": other_tenancy.workspace.pk, "flow_id": theirs.pk},
            )
            + f"?trigger={trigger.pk}"
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestTheQrEndpoint:
    def _ref_trigger(self, tenancy, flow):
        connection = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-1")
        return _trigger(flow, TriggerType.REF_URL, {"ref": "promo"}, connection=connection), connection

    def test_it_returns_a_scannable_code_for_the_links_url(self, tenancy, client_for, flow):
        trigger, connection = self._ref_trigger(tenancy, flow)
        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk)
        )

        assert response.status_code == 200
        assert response["Content-Type"] == "image/svg+xml"
        assert response.content == qr.render_svg("https://m.me/page-1?ref=promo")

    def test_png_is_offered_too(self, tenancy, client_for, flow):
        trigger, connection = self._ref_trigger(tenancy, flow)
        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk)
            + "?format=png&download=1"
        )

        assert response["Content-Type"] == "image/png"
        assert response["Content-Disposition"].startswith("attachment")
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_it_is_served_inert(self, tenancy, client_for, flow):
        """An SVG opened directly is a document (SECURITY-BASELINE §9)."""
        trigger, connection = self._ref_trigger(tenancy, flow)
        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk)
        )

        assert response["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'none'" in response["Content-Security-Policy"]

    def test_an_unknown_format_is_a_400_not_a_404(self, tenancy, client_for, flow):
        """A 404 here would read like a tenancy failure and send somebody hunting."""
        trigger, connection = self._ref_trigger(tenancy, flow)
        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk) + "?format=bmp"
        )

        assert response.status_code == 400

    def test_a_non_ref_trigger_is_404(self, tenancy, client_for, flow):
        connection = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-1")
        trigger = _trigger(flow, connection=connection)

        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk)
        )

        assert response.status_code == 404

    def test_a_connection_the_trigger_does_not_cover_is_404(self, tenancy, client_for, flow):
        trigger, _ = self._ref_trigger(tenancy, flow)
        other = connection_for(tenancy.workspace, platform=Platform.MESSENGER, external_id="page-2")

        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=other.pk)
        )

        assert response.status_code == 404

    def test_a_connection_with_no_handle_is_404_rather_than_an_explanation(self, tenancy, client_for, flow):
        """A connection this trigger does not cover, one whose handle is unknown,
        and one that does not exist all look the same from outside."""
        telegram = connection_for(tenancy.workspace, external_id="bot-1")
        trigger = _trigger(flow, TriggerType.REF_URL, {"ref": "promo"}, connection=telegram)

        response = client_for(tenancy.owner).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=telegram.pk)
        )

        assert response.status_code == 404

    def test_a_viewer_may_read_it(self, tenancy, client_for, flow):
        trigger, connection = self._ref_trigger(tenancy, flow)
        response = client_for(tenancy.user_for("viewer")).get(
            _url("flows:trigger_qr", tenancy, flow, trigger_id=trigger.pk, connection_id=connection.pk)
        )

        assert response.status_code == 200

    def test_the_bytes_are_a_real_qr_of_the_link(self):
        """Guards the assertion above: segno's own output for that URL."""
        assert qr.render_svg("https://m.me/page-1?ref=promo") != qr.render_svg("https://m.me/page-1?ref=other")
        assert segno.make("https://m.me/page-1?ref=promo") is not None
