"""A test adapter, so the framework can be exercised end to end.

Issue #4 ships no real adapter — Telegram is #12 and the rest are Layer 5 — but
"endpoint → registry → dispatch seam" is exactly the path most worth testing,
and testing it with a mock of the endpoint's own collaborator would prove
nothing. So this is a real :class:`~apps.channels.providers.base.Adapter`, using
the real signature helpers, registered through the real registry.

It deliberately mirrors the two shapes the framework has to support:

* ``resolve_connection`` from a header secret, the way Telegram identifies
  itself on the shared ``/webhooks/<platform>/`` URL;
* ``verify_webhook`` as a raw-body HMAC in a ``sha256=`` header, the way every
  Meta platform signs.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from django.http import HttpRequest

from apps.channels import security
from apps.channels.capabilities import capabilities_for
from apps.channels.events import EventPayload, EventType, NormalizedEvent, OutboundMessage, SendResult, SendStatus
from apps.channels.models import ChannelConnection
from apps.channels.providers.base import Adapter
from apps.channels.registry import register_adapter, unregister_adapter

SIGNATURE_HEADER = "X-Fake-Signature"
SECRET_HEADER = "X-Fake-Secret"


def sign(secret: str, body: bytes) -> str:
    """The header value a correctly configured platform would send."""
    return f"sha256={security.sign_body(secret, body)}"


class FakeAdapter(Adapter):
    """Accepts ``{"events": [{"id": ..., "user": ..., "text": ...}, ...]}``."""

    platform = ""
    webhook_content = "json"

    #: Set by :func:`fake_adapter_for`. Instance-level counters would be lost —
    #: the registry hands out a fresh instance per request.
    sends: list[OutboundMessage] = []

    def resolve_connection(self, request: HttpRequest, raw_body: bytes) -> ChannelConnection | None:
        return ChannelConnection.resolve_by_webhook_secret(request.headers.get(SECRET_HEADER, ""))

    def verify_webhook(self, request: HttpRequest, connection: ChannelConnection) -> bool:
        return security.verify_signature_header(
            secret=connection.webhook_secret,
            raw_body=request.body,
            header_value=request.headers.get(SIGNATURE_HEADER),
        )

    def parse_events(self, request: HttpRequest, connection: ChannelConnection) -> list[NormalizedEvent]:
        """Defensive by contract: anything unrecognisable is dropped, not raised."""
        # json_payload, not parse_json_body: the endpoint has already parsed
        # this body to validate it, and this reads that result instead of
        # decoding up to WEBHOOK_MAX_BODY_BYTES a second time. Real adapters
        # should copy this.
        payload = security.json_payload(request) or {}
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return []

        events: list[NormalizedEvent] = []
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            event_id = item.get("id")
            user = item.get("user")
            if not isinstance(event_id, str) or not isinstance(user, str) or not event_id or not user:
                continue
            text = item.get("text")
            events.append(
                NormalizedEvent(
                    type=EventType.MESSAGE,
                    connection=connection,
                    platform_user_id=user[:200],
                    # Not truncated here: the framework hashes an id that does
                    # not fit rather than cutting it, so an adapter that trims
                    # first would reintroduce the collision that fix removed.
                    provider_event_id=event_id,
                    timestamp=datetime.now(UTC),
                    payload=EventPayload(text=text if isinstance(text, str) else ""),
                    raw=item,
                )
            )
        return events

    def send(self, connection: ChannelConnection, identity: Any, outbound: OutboundMessage) -> SendResult:
        type(self).sends.append(outbound)
        return SendResult(status=SendStatus.SENT, provider_message_id="fake-1")


def fake_adapter_for(platform: str) -> type[FakeAdapter]:
    """A FakeAdapter subclass bound to ``platform``, with its own send log."""
    return type(
        f"FakeAdapter{platform.title()}",
        (FakeAdapter,),
        {"platform": platform, "capabilities": capabilities_for(platform), "sends": []},
    )


@contextmanager
def registered(platform: str) -> Iterator[type[FakeAdapter]]:
    """Register a fake adapter for ``platform`` for the duration of a test.

    The registry is process-global, so leaving one behind would leak into every
    later test in the same process — including the ones asserting that a
    platform *without* an adapter answers 503.
    """
    adapter_cls = fake_adapter_for(platform)
    register_adapter(platform, adapter_cls)
    try:
        yield adapter_cls
    finally:
        unregister_adapter(platform)
