"""Builders the engine tests share: contacts, published flows, and a fake facade.

Two things every engine test needs and neither belongs in a test module.

**A published flow at a graph.** ``create_flow`` + ``save_draft`` + ``publish``
is three calls and a validation gate, and a test that wants "a flow that sends
one message" should not spell all three.

**A stand-in for L3-A.** ROADMAP contract 1's ``apps/messaging/services.py`` is
issue #8's, developed in parallel, and this app reaches it through the single
seam at :mod:`apps.flows.messaging`. :class:`FakeFacade` records the calls that
seam makes so a test can assert on the *contract* — the argument names and the
values — rather than on a mock's spelling. When the real facade lands, a test
that passes here and fails there is a genuine signature disagreement, which is
the point of faking one module instead of six import sites.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from apps.contacts.models import Contact
from apps.flows import messaging
from apps.flows.models import Flow
from apps.flows.schema import empty_graph
from apps.flows.services import create_flow, publish, save_draft

__all__ = [
    "FakeFacade",
    "FakeMessage",
    "connection_for",
    "contact_for",
    "inbound",
    "edge",
    "graph",
    "node",
    "node_runtime",
    "published_flow",
]


def contact_for(workspace: Any, **fields: Any) -> Contact:
    """A saved contact in ``workspace``, named unless the test says otherwise."""
    fields.setdefault("first_name", "Ada")
    fields.setdefault("last_name", "Lovelace")
    return Contact.objects.create(workspace=workspace, **fields)


def node(node_id: str, node_type: str, config: dict[str, Any] | None = None, x: int = 0) -> dict[str, Any]:
    """One graph node. ``position`` is required by the envelope schema."""
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": 0}, "config": config or {}}


def edge(source: str, handle: str, target: str, edge_id: str = "") -> dict[str, Any]:
    """One graph edge, with an id the envelope schema will accept.

    Edge ids are letters, digits, ``_`` and ``-`` only, so the ``:`` in
    ``cond:true`` and ``btn:yes`` has to go — a generated id carrying one fails
    validation, and the failure surfaces as an unpublishable fixture rather than
    as anything to do with the handle.
    """
    return {
        "id": edge_id or f"{source}-{handle.replace(':', '-')}-{target}",
        "source": source,
        "sourceHandle": handle,
        "target": target,
    }


def graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    document = empty_graph()
    document["nodes"] = nodes
    document["edges"] = edges or []
    return document


def published_flow(workspace: Any, document: dict[str, Any], *, name: str = "Test flow") -> Flow:
    """A flow whose newest version is published and holds ``document``."""
    flow = create_flow(workspace=workspace, name=name)
    save_draft(flow, document)
    publish(flow)
    flow.refresh_from_db()
    return flow


@dataclass
class FakeMessage:
    """What contract 1 says ``send_outbound`` returns: a message row with a status."""

    status: str = "sent"
    error: str = ""
    provider_message_id: str = "provider-1"


@dataclass
class FakeFacade:
    """A recording stand-in for ``apps/messaging/services.py`` (contract 1).

    Install it with :meth:`install`, which patches the seam module's functions
    for the duration of a test. Every call is appended to :attr:`calls` as
    ``(name, kwargs)`` — keyword arguments throughout, because the thing worth
    asserting is that the engine passes ``source="data_collection"`` and
    ``opt_in=True``, not that it passed them third and fifth.
    """

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    result: FakeMessage = field(default_factory=FakeMessage)
    #: Set to raise from ``send_outbound`` — the "provider blew up" case, as
    #: opposed to a compliance denial, which contract 1 returns rather than raises.
    send_raises: Exception | None = None

    def install(self, monkeypatch: Any) -> "FakeFacade":
        monkeypatch.setattr(messaging, "send_outbound", self._send_outbound)
        monkeypatch.setattr(messaging, "upsert_contact_identity", self._upsert_contact_identity)
        monkeypatch.setattr(messaging, "open_conversation", self._recorder("open_conversation"))
        monkeypatch.setattr(messaging, "close_conversation", self._recorder("close_conversation"))
        monkeypatch.setattr(messaging, "assign_conversation", self._recorder("assign_conversation"))
        monkeypatch.setattr(messaging, "pause_automation", self._recorder("pause_automation"))
        monkeypatch.setattr(messaging, "available", lambda: True)
        return self

    def _recorder(self, name: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, {"args": args, **kwargs}))

        return _call

    def _send_outbound(self, **kwargs: Any) -> FakeMessage:
        self.calls.append(("send_outbound", kwargs))
        if self.send_raises is not None:
            raise self.send_raises
        return self.result

    def _upsert_contact_identity(self, contact: Any, platform: str, address: str, **kwargs: Any) -> None:
        self.calls.append(
            ("upsert_contact_identity", {"contact": contact, "platform": platform, "address": address, **kwargs})
        )

    def named(self, name: str) -> list[dict[str, Any]]:
        """Every recorded call to ``name``, in order."""
        return [kwargs for called, kwargs in self.calls if called == name]


@contextmanager
def node_runtime(
    node_type: str,
    execute: Callable[[Any], Any],
    *,
    synchronous_safe: bool = True,
) -> Iterator[None]:
    """Run ``node_type`` with ``execute`` for the duration of the block.

    The runner's contract is "a node returns one of five results and the runner
    does the rest", and testing *that* needs a node that returns the result the
    test is about — not a real node coaxed into returning it. Swapping the
    runtime rather than the graph also keeps the fixture graph publishable,
    which matters: a graph that cannot pass validation cannot reach the runner
    at all, so a test built on one would be testing nothing.

    Restores the real class on the way out, including when the body raises. The
    registry is process-global, so a leaked stub would fail an unrelated test in
    a later module and be miserable to trace.
    """
    from apps.flows.engine.nodes.base import Node
    from apps.flows.engine.registry import node_class_for, register_node, unregister_node

    original = node_class_for(node_type)
    stub = type(
        "StubNode",
        (Node,),
        {
            "type": node_type,
            "synchronous_safe": synchronous_safe,
            "execute": lambda self, ctx: execute(ctx),
        },
    )
    register_node(stub, replace=True)
    try:
        yield
    finally:
        if original is not None:
            register_node(original, replace=True)
        else:
            # No runtime to put back: the type is one a later PR or layer owns
            # (send_message until #9's PR 2, external_request until L4-E), so
            # leaving the stub would tell the next test module this deployment
            # implements it.
            unregister_node(node_type)


def connection_for(workspace: Any, *, platform: str = "telegram", external_id: str = "bot-1") -> Any:
    """A channel connection to run an execution on.

    Executions carry one because contract 1 needs it on every send and SPEC §9.3
    routes replies by it, so most engine tests that send anything need one too.
    """
    from apps.channels.models import ChannelConnection

    return ChannelConnection.objects.create(
        workspace=workspace,
        platform=platform,
        display_name=f"{platform} test",
        external_id=external_id,
    )


def inbound(
    connection: Any,
    *,
    text: str = "",
    button_id: str = "",
    event_id: str = "evt-1",
    kind: Any = None,
    ref: str = "",
    user: str = "tg-1",
    comment_id: str = "",
    extra: dict[str, Any] | None = None,
) -> Any:
    """One inbound ``NormalizedEvent``, the shape L4-A hands ``attempt_resume``.

    Built from ``apps.channels.events`` rather than a stand-in: the matching
    logic reads ``payload.button_id`` and ``payload.text``, and a duck-typed
    double would keep passing if either name changed.

    ``kind`` names the event type explicitly. Without it the type is inferred
    from what was passed, which covers the two shapes the engine tests need and
    none of the five issue #11 routes on.
    """
    from django.utils import timezone

    from apps.channels.events import EventPayload, EventType, NormalizedEvent

    if kind is None:
        kind = EventType.POSTBACK if button_id else EventType.MESSAGE
    return NormalizedEvent(
        type=kind,
        connection=connection,
        platform_user_id=user,
        provider_event_id=event_id,
        timestamp=timezone.now(),
        payload=EventPayload(
            text=text,
            button_id=button_id,
            ref=ref,
            comment_id=comment_id,
            extra=dict(extra or {}),
        ),
    )
