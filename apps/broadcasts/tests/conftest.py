"""Fixtures for the broadcast suite.

Real objects throughout — a real ``ChannelConnection``, real identities, the real
condition engine and the real ``FakeAdapter`` from ``apps.channels.tests`` — for
the reason that module's docstring gives: the path worth testing here is
audience → compliance → queue → facade → adapter, and a mock of any link in it
proves nothing about the others.
"""

from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.broadcasts import services
from apps.broadcasts.models import Broadcast
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.tests.fake_adapter import fake_adapter_for, swapped_adapter
from apps.common.platforms import Platform
from apps.contacts.models import Contact
from apps.messaging.models import ContactChannelIdentity, OptInSource

#: A filter document matching every active contact in the workspace. ``any`` with
#: no rules matches nobody and ``all`` with no rules matches everyone, so this is
#: written out rather than left empty — the composer refuses an empty document
#: and the tests should be explicit about which of the two they mean.
EVERYONE: dict[str, Any] = {"match": "all", "rules": []}


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    """A Telegram connection: no messaging window, so eligibility is unclouded."""
    return ChannelConnection.objects.create(
        workspace=tenancy.workspace,
        platform=Platform.TELEGRAM,
        display_name="Test bot",
        external_id=f"bot-{tenancy.slug}",
        status=ConnectionStatus.ACTIVE,
    )


@pytest.fixture
def messenger_connection(tenancy: Any) -> ChannelConnection:
    """A Messenger connection: a 24-hour window whose escape is a message tag."""
    return ChannelConnection.objects.create(
        workspace=tenancy.workspace,
        platform=Platform.MESSENGER,
        display_name="Test page",
        external_id=f"page-{tenancy.slug}",
        status=ConnectionStatus.ACTIVE,
    )


@pytest.fixture
def whatsapp_connection(tenancy: Any) -> ChannelConnection:
    """A WhatsApp connection: a 24-hour window whose escape is a template."""
    return ChannelConnection.objects.create(
        workspace=tenancy.workspace,
        platform=Platform.WHATSAPP,
        display_name="Test number",
        external_id=f"wa-{tenancy.slug}",
        status=ConnectionStatus.ACTIVE,
    )


@pytest.fixture
def make_contacts(tenancy: Any) -> Callable[..., list[Contact]]:
    """``make_contacts(n, connection=...)`` → contacts each with one identity.

    ``window`` controls the messaging window on the identity, which is the only
    thing that separates an eligible contact from a ``needs_tag`` one on a
    windowed platform. ``opted_out`` and ``opt_in`` cover the two denials that
    apply everywhere.
    """

    def _make(
        count: int,
        *,
        connection: ChannelConnection,
        window: timedelta | None = timedelta(hours=1),
        opted_out: bool = False,
        opt_in: bool = True,
        identity: bool = True,
        prefix: str = "c",
    ) -> list[Contact]:
        now = timezone.now()
        # bulk_create, because the acceptance tests build twelve hundred of these
        # and two statements beat twenty-four hundred. It bypasses ``save()``, so
        # the columns those overrides would derive — ``workspace`` on both models
        # — are set explicitly here. Everything else is the ordinary model.
        contacts = Contact.objects.bulk_create(
            [
                Contact(
                    workspace=tenancy.workspace,
                    first_name=f"{prefix}{index}",
                    email=f"{prefix}{index}@example.test",
                )
                for index in range(count)
            ]
        )
        if identity:
            ContactChannelIdentity.objects.bulk_create(
                [
                    ContactChannelIdentity(
                        workspace=tenancy.workspace,
                        contact=contact,
                        channel_connection=connection,
                        platform=connection.platform,
                        platform_user_id=f"{prefix}-{connection.pk}-{index}",
                        opt_in=opt_in,
                        opt_in_at=now if opt_in else None,
                        opt_in_source=OptInSource.MESSAGE_IN if opt_in else "",
                        opted_out_at=now if opted_out else None,
                        window_expires_at=(now + window) if window else None,
                        last_inbound_at=now if window else None,
                    )
                    for index, contact in enumerate(contacts)
                ]
            )
        return list(contacts)

    return _make


@pytest.fixture
def make_broadcast(tenancy: Any) -> Callable[..., Broadcast]:
    """``make_broadcast(connection=...)`` → a draft with an audience and a message."""

    def _make(
        *,
        connection: ChannelConnection,
        text: str = "Hello there",
        filter_json: Any = None,
        tag: str = "",
        name: str = "Spring sale",
    ) -> Broadcast:
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name=name, connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE if filter_json is None else filter_json)
        services.save_content(broadcast, {"blocks": [{"type": "text", "text": text}]}, user=tenancy.owner)
        if tag:
            services.set_tag(broadcast, tag)
        broadcast.refresh_from_db()
        return broadcast

    return _make


@pytest.fixture
def adapter_for() -> Callable[[str], Any]:
    """``with adapter_for(platform) as adapter:`` → the fake adapter's send log.

    Registered through the real registry, so the send path under test is the one
    the product runs: facade → bucket → adapter.
    """
    from contextlib import contextmanager

    @contextmanager
    def _adapter(platform: str) -> Iterator[Any]:
        adapter_cls = fake_adapter_for(platform)
        with swapped_adapter(platform, adapter_cls):
            yield adapter_cls

    return _adapter
