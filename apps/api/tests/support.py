"""A stand-in internet for the outbound-webhook tests.

The same shape ``apps/flows/tests/test_node_external_request.py`` uses, and for
the same reason: the delivery path builds its own client (it takes a URL, not a
transport), so the seam is ``httpx.HTTPTransport.handle_request`` rather than a
``MockTransport`` client. DNS is replaced at ``apps.common.outbound.resolve_host``,
the one place the guard resolves anything.

**The guard itself is left entirely real.** That is the point — a webhook test
that stubbed ``guarded_request`` would prove nothing about SSRF, and
``tests/ssrf.py::guard_required`` exists precisely because asserting on a patched
guard stays green when a second, unguarded request is made beside it.
"""

import ipaddress
from collections.abc import Callable
from typing import Any

import httpx

from apps.common import outbound

RECEIVER = "receiver.example.com"
PUBLIC = "93.184.216.34"

__all__ = ["PUBLIC", "RECEIVER", "FakeInternet", "serving"]


class FakeInternet:
    """DNS and the socket, replaced; the guard untouched."""

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        names: dict[str, list[str]] | None = None,
    ) -> None:
        self.handler = handler
        self.names = names if names is not None else {RECEIVER: [PUBLIC]}
        self.requests: list[httpx.Request] = []

    def install(self, monkeypatch: Any) -> "FakeInternet":
        monkeypatch.setattr(outbound, "resolve_host", self._resolve)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", self._handle)
        return self

    def _resolve(self, host: str) -> tuple[Any, ...]:
        return tuple(ipaddress.ip_address(value) for value in self.names.get(host.lower(), []))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # Patched onto the class as an already-bound method, so ``self`` is the
        # FakeInternet and the transport instance never arrives — fine, since a
        # canned response does not need one.
        self.requests.append(request)
        return self.handler(request)


def serving(status: int = 200, **kwargs: Any) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **kwargs)

    return _handler


def refusing(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A receiver that cannot be reached at all."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return _handler


def drain_webhook_deliveries(workspace: Any) -> None:
    """Run the queued outbound-webhook rows the way the worker would.

    Filtered to the delivery action type on purpose. Draining the whole queue
    would also run whatever else the scenario scheduled, and this exists to move
    deliveries rather than to be a worker — ``apps/broadcasts/tests/test_fanout.py``
    explains the same choice at more length for its own drain.

    Lives here rather than in a test module because two suites need it: the
    phase-3 API scenario and ``tests/acceptance/test_integration_chain.py``. Two
    copies of a helper that marks rows DONE and calls a handler drift the moment
    the handler's signature moves, and only one of them gets updated.
    """
    from apps.api.delivery import ACTION_TYPE, handle_webhook_delivery
    from apps.queueing.models import ActionStatus, ScheduledAction

    for action in list(
        ScheduledAction.objects.for_workspace(workspace).filter(type=ACTION_TYPE, status=ActionStatus.PENDING)
    ):
        action.status = ActionStatus.DONE
        action.save(update_fields=["status"])
        handle_webhook_delivery(action.payload, action)
