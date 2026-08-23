"""Builders the routing tests share.

The adapter subclasses ``apps/channels/tests/fake_adapter.py`` rather than
replacing it: that one is a *real* ``Adapter`` using the real signature helpers
and the real registry, which is what makes an end-to-end routing test prove
something. What is added here is a recorder for ``mark_seen``/``send_typing``
(this issue is their first caller anywhere) and an injectable delay, so the
inline budget can be tested against a platform that is genuinely slow rather
than against a mock of one.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from apps.channels.capabilities import capabilities_for
from apps.channels.registry import entry_for, register_adapter, unregister_adapter
from apps.channels.tests.fake_adapter import FakeAdapter

__all__ = ["RoutingFakeAdapter", "routing_adapter"]


class RoutingFakeAdapter(FakeAdapter):
    """A fake adapter that records the courtesy calls and can be made slow."""

    #: Seconds each outbound call sleeps. Class-level, like ``sends``: the
    #: registry hands out a fresh instance per use, so an instance attribute
    #: would be lost between the courtesy call and the send.
    delay: float = 0.0
    courtesies: list[tuple[str, str]] = []

    def mark_seen(self, connection: Any, identity: Any) -> None:
        type(self).courtesies.append(("mark_seen", str(connection.pk)))
        self._pause()

    def send_typing(self, connection: Any, identity: Any) -> None:
        type(self).courtesies.append(("send_typing", str(connection.pk)))
        self._pause()

    def send(self, connection: Any, identity: Any, outbound: Any) -> Any:
        self._pause()
        return super().send(connection, identity, outbound)

    def _pause(self) -> None:
        if type(self).delay:
            time.sleep(type(self).delay)


@contextmanager
def routing_adapter(platform: str, *, delay: float = 0.0) -> Iterator[type[RoutingFakeAdapter]]:
    """Register a routing-aware fake adapter for the duration of a test.

    Saves and restores whatever occupied the slot, the same way
    ``apps.channels.tests.fake_adapter.registered`` does and for the same
    reason: since issue #12 a real Telegram adapter is registered in every
    process by ``ChannelsConfig.ready()``, so registering over it hits the
    duplicate guard on the way in and clearing the slot on the way out leaves
    the rest of the run with no Telegram adapter at all. Correct for an
    occupied slot and an empty one alike.
    """
    adapter_cls: type[RoutingFakeAdapter] = type(
        f"RoutingFakeAdapter{platform.title()}",
        (RoutingFakeAdapter,),
        {
            "platform": platform,
            "capabilities": capabilities_for(platform),
            "sends": [],
            "courtesies": [],
            "delay": delay,
        },
    )
    previous = entry_for(platform).adapter_cls
    unregister_adapter(platform)
    register_adapter(platform, adapter_cls)
    try:
        yield adapter_cls
    finally:
        unregister_adapter(platform)
        if previous is not None:
            register_adapter(platform, previous)
