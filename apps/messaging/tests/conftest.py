"""Fixtures shared by the messaging test modules."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from apps.channels import ingest as channels_ingest
from apps.channels.events import EventPayload, EventType, NormalizedEvent
from apps.channels.models import ChannelConnection
from apps.common.platforms import Platform
from apps.messaging.ingest import register_processors


@pytest.fixture(autouse=True)
def _registered_processors() -> Iterator[None]:
    """Guarantee this app's own seam registration, and leave the rest alone.

    ``MessagingConfig.ready()`` registers persistence and the routing tail once
    per process, so in a normal run these are already installed — but the
    channels suite clears the registry for the duration of each of its tests
    (see ``apps/channels/tests/conftest.py``), and pytest ordering is not a
    contract. Re-registering is idempotent: the seam replaces under a name
    rather than stacking.

    Teardown restores the **snapshot**, and deliberately does not keep a list of
    the stage names that are allowed to exist. It used to: it dropped everything
    that was not persistence or routing, on the reasoning that those were what
    ``ready()`` leaves. That stopped being true the moment a third app
    registered a stage — SPEC §16's flow preview, from
    ``apps.flows.apps.FlowsConfig.ready()`` — and the symptom was this suite
    silently deleting it for the rest of the process, so a test in another app
    that asserted the stage exists failed only when it ran after this one.
    """
    register_processors()
    # Reaching for the private registry mirrors what the channels suite does,
    # and for the same reason: restoring needs the callables, and the public
    # surface exposes only names.
    snapshot = {name: channels_ingest._PROCESSORS[name] for name in channels_ingest.registered_processors()}
    yield
    for name in channels_ingest.registered_processors():
        channels_ingest.unregister_processor(name)
    for name, processor in snapshot.items():
        channels_ingest.register_processor(processor, name=name)


def make_connection(workspace: Any, *, platform: str = Platform.TELEGRAM, suffix: str = "") -> ChannelConnection:
    """An active connection. ``external_id`` is namespaced: SPEC §5's unique on
    ``(platform, external_id)`` is deployment-wide, so a fixed literal would make
    two tenancies in one test collide."""
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
    """An active Telegram connection in the victim tenancy."""
    return make_connection(tenancy.workspace)


def make_event(
    connection: ChannelConnection,
    *,
    user: str = "u1",
    event_id: str = "e1",
    text: str = "hello",
    kind: str = EventType.MESSAGE,
    payload: EventPayload | None = None,
    timestamp: datetime | None = None,
) -> NormalizedEvent:
    return NormalizedEvent(
        type=EventType(kind),
        connection=connection,
        platform_user_id=user,
        provider_event_id=event_id,
        timestamp=timestamp or datetime.now(UTC),
        payload=payload if payload is not None else EventPayload(text=text),
    )
