"""Fixtures shared by the channels test modules."""

from collections.abc import Iterator
from typing import Any

import pytest

from apps.channels.models import ChannelConnection
from apps.channels.tests.fake_adapter import FakeAdapter, registered
from apps.common.platforms import Platform


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    """An active Telegram connection in the victim tenancy, secret already set."""
    obj = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.TELEGRAM,
        display_name="Support bot",
        external_id="bot-acme",
    )
    obj.rotate_webhook_secret()
    obj.save()
    return obj


@pytest.fixture
def secret(connection: ChannelConnection) -> str:
    """The connection's webhook secret, in plaintext."""
    return connection.webhook_secret


@pytest.fixture
def fake_adapter() -> Iterator[type[FakeAdapter]]:
    """A registered fake Telegram adapter, removed again afterwards."""
    with registered(Platform.TELEGRAM) as adapter_cls:
        yield adapter_cls


@pytest.fixture(autouse=True)
def _clean_processors() -> Iterator[None]:
    """Leave the contract-6 seam as empty as it was found.

    The processor registry is module-global. A test that registers one and does
    not remove it would silently change every later test's dispatch behaviour —
    including the ones asserting the default no-op.
    """
    from apps.channels import ingest

    before = tuple(ingest.registered_processors())
    yield
    for name in ingest.registered_processors():
        if name not in before:
            ingest.unregister_processor(name)
