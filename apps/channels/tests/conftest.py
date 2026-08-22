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
    """Run every test in this app against an EMPTY contract-6 seam.

    The processor registry is module-global, and two different leaks have to be
    prevented here.

    A test that registers a processor and does not remove it would silently
    change every later test's dispatch behaviour — including the ones asserting
    the default no-op. That is the outward half.

    The inward half arrived with issue #8: ``MessagingConfig.ready()`` registers
    persistence and a routing stage for real, in every process, so by the time
    these tests run the seam is no longer empty. Snapshot-and-restore alone
    would leave them asserting on whatever apps happen to be installed, and
    running each dispatch through the messaging spine — which is L3-A's own
    tests' job, not this app's. So the registry is cleared for the duration and
    put back exactly as it was, and these tests go on testing the seam itself.
    """
    from apps.channels import ingest

    # Reaching for the private registry is deliberate and stays inside this
    # app's own test suite: restoring requires the callables, and the public
    # surface deliberately exposes only names.
    before = {name: ingest._PROCESSORS[name] for name in ingest.registered_processors()}
    for name in before:
        ingest.unregister_processor(name)
    try:
        yield
    finally:
        for name in ingest.registered_processors():
            ingest.unregister_processor(name)
        for name, processor in before.items():
            ingest.register_processor(processor, name=name)
