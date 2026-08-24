"""Fixtures the analytics test modules share.

Everything here builds *real* rows through the real services, because what these
tests are about is a counter agreeing with what the rest of the product did. A
faked send would let the counters be right about a thing that never happened.
"""

from typing import Any

import pytest

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.flows.fixtures import graph_for
from apps.flows.models import FlowExecution, StartedBy
from apps.flows.services import latest_version
from apps.flows.tests.support import published_flow
from apps.messaging.models import ContactChannelIdentity, OptInSource

TEXT = OutboundMessage(blocks=(TextBlock(text="hello"),))


def make_connection(workspace: Any, *, platform: str = Platform.TELEGRAM, suffix: str = "") -> ChannelConnection:
    """An active connection. ``external_id`` is namespaced because SPEC §5's
    unique on ``(platform, external_id)`` is deployment-wide."""
    connection = ChannelConnection(
        workspace=workspace,
        platform=platform,
        display_name=f"{platform} {suffix or workspace.pk}",
        external_id=f"{platform}-{suffix or workspace.pk}",
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    return make_connection(tenancy.workspace)


@pytest.fixture
def contact(tenancy: Any) -> Any:
    return create_contact(tenancy.workspace, first_name="Ada")


@pytest.fixture
def identity(contact: Any, connection: Any) -> ContactChannelIdentity:
    from django.utils import timezone

    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=connection.platform,
        platform_user_id="u1",
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source=OptInSource.MESSAGE_IN,
        last_inbound_at=timezone.now(),
    )


#: The entry node id in ``graph_for``'s fixture graphs. Named here so the tests
#: read as "the node that sends" rather than as a literal repeated twenty times.
ENTRY_NODE = "subject"


@pytest.fixture
def flow(tenancy: Any) -> Any:
    """A published flow whose entry node sends one message."""
    return published_flow(tenancy.workspace, graph_for("send_message"))


def make_execution(flow: Any, contact: Any, connection: Any = None, *, preview: bool = False) -> FlowExecution:
    execution = FlowExecution(
        flow=flow,
        flow_version=latest_version(flow),
        contact=contact,
        channel_connection=connection,
        current_node_id=ENTRY_NODE,
        started_by=StartedBy.stamp(StartedBy.MANUAL),
        preview=preview,
    )
    execution.save()
    return execution


@pytest.fixture
def execution(flow: Any, contact: Any, connection: Any) -> FlowExecution:
    return make_execution(flow, contact, connection)
