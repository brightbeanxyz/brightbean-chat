"""The ``send_email`` node's own tracking call (issue #26).

This node deliberately does **not** go through ``apps.flows.engine.sending.deliver``
— its module docstring explains why: it resolves the workspace's *email*
connection rather than the one the run is happening on. So the tracking call is
made twice in the codebase, and this file is what stops the second one from
being quietly dropped: an email that carries neither a wrapped link nor a pixel
is a counter that silently reads zero forever.
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.models import TrackingSettings
from apps.channels.models import ChannelConnection
from apps.channels.providers import email_backends
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.flows.engine.context import NodeContext
from apps.flows.engine.graph import Graph
from apps.flows.engine.registry import node_class_for
from apps.flows.models import FlowExecution
from apps.flows.tests.support import graph, node, published_flow
from apps.messaging.models import ContactChannelIdentity, Message

pytestmark = pytest.mark.django_db

CONFIG = {"subject": "Hello", "html_body": '<p><a href="https://example.test/docs">Docs</a></p>'}


@pytest.fixture
def email_connection(tenancy: Any) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="tracking-sender.test",
    )
    connection.credentials = {  # type: ignore[assignment]
        "provider": "smtp",
        "host": "mail.test",
        "security": "none",
        "from_address": "hello@sender.test",
        "from_name": "Sender",
    }
    connection.save()
    return connection


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    envelopes: list[Any] = []

    def record(connection: Any, envelope: Any) -> str:
        envelopes.append(envelope)
        return "id-1"

    monkeypatch.setattr(email_backends, "deliver", record)
    return envelopes


def run_email_node(tenancy: Any, email_connection: ChannelConnection, *, preview: bool = False) -> FlowExecution:
    contact = create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")
    ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=email_connection,
        platform=Platform.EMAIL.value,
        platform_user_id=contact.email,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="data_collection",
    )
    flow = published_flow(tenancy.workspace, graph([node("n1", "send_email", CONFIG)]))
    version = flow.versions.get(published=True)
    execution = FlowExecution.objects.create(
        workspace=tenancy.workspace,
        flow_version=version,
        contact=contact,
        current_node_id="n1",
        status="running",
        preview=preview,
    )
    node_class = node_class_for("send_email")
    assert node_class is not None
    node_class().execute(
        NodeContext(
            execution=execution,
            graph=Graph(version.graph_json),
            node_id="n1",
            node_type="send_email",
            config=CONFIG,
            variables={},
        )
    )
    return execution


def stored_html(tenancy: Any) -> str:
    message = Message.objects.for_workspace(tenancy.workspace).order_by("-created_at").first()
    assert message is not None
    return str(message.body.get("html_body") or "")


class TestSendEmailNodeTracking:
    def test_it_wraps_links_and_adds_the_pixel_when_the_workspace_opts_in(
        self, tenancy: Any, email_connection: Any, delivered: Any
    ) -> None:
        TrackingSettings.objects.create(workspace=tenancy.workspace, wrap_email_links=True, open_pixel=True)

        run_email_node(tenancy, email_connection)

        body = stored_html(tenancy)
        assert "/c/" in body
        assert "/o/" in body
        # And it is what actually left the building, not only what was stored.
        assert "/c/" in delivered[0].html

    def test_it_rewrites_nothing_by_default(self, tenancy: Any, email_connection: Any, delivered: Any) -> None:
        run_email_node(tenancy, email_connection)

        body = stored_html(tenancy)
        assert "/c/" not in body
        assert "/o/" not in body
        assert "https://example.test/docs" in body

    def test_a_preview_run_is_never_rewritten(self, tenancy: Any, email_connection: Any, delivered: Any) -> None:
        TrackingSettings.objects.create(workspace=tenancy.workspace, wrap_email_links=True, open_pixel=True)

        run_email_node(tenancy, email_connection, preview=True)

        body = stored_html(tenancy)
        assert "/c/" not in body
        assert "/o/" not in body
