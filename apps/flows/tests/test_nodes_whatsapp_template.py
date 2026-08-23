"""The ``whatsapp_template`` half of ``send_message`` (SPEC §6.5, issue #19).

Outside WhatsApp's 24-hour window a send needs an approved template and nothing
else will do. The *decision* is the compliance engine reading a policy row; what
this config key does is supply the material, and what these tests hold it to is:

* the pair it produces — ``template_ref`` plus ``template_variables`` — is
  platform-neutral, so nothing in the flow engine branches on a platform;
* the slot values are rendered **here**, through the one shared renderer
  (SECURITY-BASELINE §3), so the adapter receives finished strings;
* a node without the key behaves exactly as it always did.
"""

from typing import Any

import pytest

from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import (
    FakeFacade,
    connection_for,
    contact_for,
    graph,
    node,
    published_flow,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def facade(monkeypatch: Any) -> FakeFacade:
    return FakeFacade().install(monkeypatch)


def run(workspace: Any, config: dict[str, Any], **kwargs: Any) -> Any:
    document = graph([node("m", "send_message", config)])
    flow = published_flow(workspace, document)
    contact = kwargs.pop("contact", None) or contact_for(workspace)
    return start(contact, flow, workspace, **kwargs)


def start(contact: Any, flow: Any, workspace: Any, **kwargs: Any) -> Any:
    from apps.flows.engine import start_flow

    return start_flow(
        contact,
        flow,
        started_by=StartedBy.API,
        connection=kwargs.pop("connection", None) or connection_for(workspace),
        **kwargs,
    )


def run_draft(workspace: Any, config: dict[str, Any]) -> Any:
    """Run a graph the publish gate would reject, the way #12's preview can.

    The runtime guards below are unreachable through a published flow — the
    schema catches a malformed slot and a key of the wrong type. ``save_draft``
    does not validate (SPEC §16 autosaves every two seconds, mid-edit), so a
    draft preview is exactly how such a graph reaches the engine, and exactly
    why the guards exist.
    """
    from apps.flows.engine import start_flow
    from apps.flows.services import save_draft

    flow = published_flow(workspace, graph([node("ok", "action", {"actions": [{"verb": "add_tag", "tag": "x"}]})]))
    version = save_draft(flow, graph([node("m", "send_message", config)]))
    return start_flow(
        contact_for(workspace),
        flow,
        started_by=StartedBy.PREVIEW,
        flow_version=version,
        connection=connection_for(workspace),
    )


def sent(facade: FakeFacade) -> Any:
    calls = facade.named("send_outbound")
    assert calls, "nothing was sent"
    return calls[-1]["outbound"]


def template_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "blocks": [{"type": "text", "text": "Hi {{first_name}}, order 42 shipped."}],
        "whatsapp_template": {
            "template_id": "01234567-89ab-7000-8000-000000000000",
            "reference": "order_shipped/en_US",
            "variables": [
                {"slot": "body.1", "value": "{{first_name}}"},
                {"slot": "body.2", "value": "42"},
            ],
        },
    }
    config.update(overrides)
    return config


class TestTheTemplatePicker:
    def test_the_reference_and_the_rendered_values_reach_the_message(self, tenancy: Any, facade: Any) -> None:
        execution = run(tenancy.workspace, template_config())

        assert execution.status == ExecutionStatus.COMPLETED
        outbound = sent(facade)
        assert outbound.template_ref == "order_shipped/en_US"
        assert outbound.template_variables == (("body.1", "Ada"), ("body.2", "42"))

    def test_a_value_is_rendered_by_the_shared_renderer_not_a_template_engine(self, tenancy: Any, facade: Any) -> None:
        """SECURITY-BASELINE §3. A contact whose name is itself a placeholder
        renders as that literal text: ``re.sub`` never rescans what a
        replacement callable returned."""
        from apps.contacts.services import create_contact

        contact = create_contact(workspace=tenancy.workspace, first_name="{{last_name}}", last_name="Byron")
        run(tenancy.workspace, template_config(), contact=contact)
        assert sent(facade).template_variables[0] == ("body.1", "{{last_name}}")

    def test_the_blocks_still_go_to_the_thread(self, tenancy: Any, facade: Any) -> None:
        """The adapter puts only the template on the wire; the block is what the
        inbox shows, so the two have to agree and both have to be present."""
        run(tenancy.workspace, template_config())
        outbound = sent(facade)
        assert outbound.blocks[0].text == "Hi Ada, order 42 shipped."
        assert outbound.template_ref

    def test_a_node_without_the_key_carries_no_template(self, tenancy: Any, facade: Any) -> None:
        run(tenancy.workspace, {"blocks": [{"type": "text", "text": "Hi"}]})
        outbound = sent(facade)
        assert outbound.template_ref is None
        assert outbound.template_variables == ()

    def test_a_template_with_no_variables_is_still_a_template(self, tenancy: Any, facade: Any) -> None:
        config = template_config()
        config["whatsapp_template"]["variables"] = []
        run(tenancy.workspace, config)
        assert sent(facade).template_ref == "order_shipped/en_US"

    def test_a_malformed_entry_is_skipped_rather_than_fatal(self, tenancy: Any, facade: Any) -> None:
        """Schema validation runs at publish; a draft preview reaches the engine
        unvalidated (SPEC §16 autosaves mid-edit)."""
        config = template_config()
        config["whatsapp_template"]["variables"] = [{"slot": "", "value": "x"}, {"slot": "body.1", "value": "ok"}]
        run_draft(tenancy.workspace, config)
        assert sent(facade).template_variables == (("body.1", "ok"),)

    def test_a_key_of_the_wrong_shape_is_ignored(self, tenancy: Any, facade: Any) -> None:
        run_draft(tenancy.workspace, template_config(whatsapp_template="not-a-dict"))
        assert sent(facade).template_ref is None


class TestTheSchemaAcceptsIt:
    def test_a_graph_with_a_template_validates(self, tenancy: Any) -> None:
        from apps.flows.schema.validation import validate_graph

        result = validate_graph(graph([node("m", "send_message", template_config())]))
        assert result.is_publishable, result.errors

    def test_an_unknown_key_inside_the_template_is_refused(self, tenancy: Any) -> None:
        """Every object in the schema is closed (SECURITY-BASELINE §7)."""
        from apps.flows.schema.validation import validate_graph

        config = template_config()
        config["whatsapp_template"]["surprise"] = "x"
        result = validate_graph(graph([node("m", "send_message", config)]))
        assert result.errors

    def test_a_slot_that_is_not_a_slot_is_refused(self, tenancy: Any) -> None:
        from apps.flows.schema.validation import validate_graph

        config = template_config()
        config["whatsapp_template"]["variables"] = [{"slot": "../../etc", "value": "x"}]
        result = validate_graph(graph([node("m", "send_message", config)]))
        assert result.errors

    def test_a_reference_that_is_not_name_slash_language_is_refused(self, tenancy: Any) -> None:
        from apps.flows.schema.validation import validate_graph

        config = template_config()
        config["whatsapp_template"]["reference"] = "Order Shipped"
        result = validate_graph(graph([node("m", "send_message", config)]))
        assert result.errors
