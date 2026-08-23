"""Fixtures for the flows suite, and the seam hygiene issue #11 needs.

Two module-global registries are involved in routing, and both leak between
tests if nobody puts them back.

The **processor seam** is shared with two other apps that also manage it:
``apps/channels/tests/conftest.py`` empties it for the duration of each of its
tests, and ``apps/messaging/tests/conftest.py`` calls ``register_processors()``,
which — because of its own guard — reinstalls messaging's *no-op* under
``"routing"`` whenever the slot is free. Either can leave this app's tests
running against a router that is not this app's. So they re-claim it.

The **hook registry** is this issue's own, and a test that registers a probe hook
would otherwise change every later test's stage chain.
"""

from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _routing_registered() -> Iterator[None]:
    """Guarantee the real router and the built-in hooks, whatever ran before."""
    from apps.channels import ingest as channels_ingest
    from apps.flows.triggers import hooks
    from apps.flows.triggers.pipeline import ROUTING_PROCESSOR, register_routing, route_events
    from apps.flows.triggers.stages import register_builtin_hooks

    # Snapshotted and restored through the public API, so the registry carries
    # no test-only escape hatch of its own.
    previous = hooks.registered_hooks()
    for registration in previous:
        hooks.unregister_hook(registration.name)
    register_builtin_hooks()
    register_routing()
    try:
        yield
    finally:
        for registration in hooks.registered_hooks():
            hooks.unregister_hook(registration.name)
        for registration in previous:
            hooks.register_hook(
                registration.hook,
                stage=registration.stage,
                name=registration.name,
                priority=registration.priority,
                replace_existing=True,
            )
        if channels_ingest.registered_processors():
            # Only re-assert the slot when the seam is populated at all: an app
            # that cleared it deliberately gets to keep it cleared.
            channels_ingest.register_processor(route_events, name=ROUTING_PROCESSOR)


@pytest.fixture
def connection(tenancy: Any) -> Any:
    """An active Telegram connection in the victim tenancy."""
    from apps.flows.tests.support import connection_for

    return connection_for(tenancy.workspace, external_id=f"bot-{tenancy.slug}")


@pytest.fixture
def contact(tenancy: Any) -> Any:
    from apps.flows.tests.support import contact_for

    return contact_for(tenancy.workspace)
