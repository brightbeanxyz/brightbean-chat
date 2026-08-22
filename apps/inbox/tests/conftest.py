"""Fixtures for the inbox suite.

Everything reachable is reused rather than rebuilt: ``make_connection`` comes
from the messaging suite, the hostile corpus from ``apps/messaging/tests/hostile.py``
(whose docstring says it was exported for this issue), and the fake adapter from
the channels suite — which is what lets a test assert that an internal note
never reaches a provider.
"""

from collections.abc import Callable
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.contacts.models import Contact
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    OptInSource,
)
from apps.messaging.services import open_conversation
from apps.messaging.tests.conftest import make_connection

__all__ = [
    "agent_client",
    "connection",
    "contact",
    "conversation",
    "identity",
    "inbound",
    "make_connection",
    "outbound",
    "url_for",
    "viewer_client",
]


@pytest.fixture
def connection(tenancy: Any) -> Any:
    """An active Telegram connection in the victim tenancy."""
    return make_connection(tenancy.workspace)


@pytest.fixture
def contact(tenancy: Any) -> Contact:
    return Contact.objects.create(workspace=tenancy.workspace, first_name="Ada", last_name="Lovelace")


@pytest.fixture
def identity(contact: Contact, connection: Any) -> ContactChannelIdentity:
    """An opted-in identity with an open messaging window."""
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


@pytest.fixture
def conversation(tenancy: Any, contact: Contact, connection: Any) -> Conversation:
    """Through the facade, not ``objects.create``: it is the product's own path."""
    return open_conversation(workspace=tenancy.workspace, contact=contact, connection=connection)


@pytest.fixture
def inbound(conversation: Conversation) -> Callable[..., Message]:
    """Make an inbound message the way ``messaging.ingest`` does."""

    def _inbound(text: str = "hello", *, key: str = "", blocks: Any = None) -> Message:
        body = {"blocks": blocks if blocks is not None else [{"type": "text", "text": text}]}
        message = Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.IN,
            status=MessageStatus.DELIVERED,
            body=body,
            idempotency_key=key or f"in:{timezone.now().timestamp()}",
        )
        conversation.last_message_at = message.created_at
        conversation.save(update_fields=["last_message_at", "updated_at"])
        return message

    return _inbound


@pytest.fixture
def outbound(conversation: Conversation) -> Callable[..., Message]:
    """Make an outbound row directly, for rendering and retry tests."""

    def _outbound(
        text: str = "hi",
        *,
        status: str = MessageStatus.SENT,
        error: str = "",
        internal: bool = False,
        key: str = "",
        blocks: Any = None,
    ) -> Message:
        body = {"blocks": blocks if blocks is not None else [{"type": "text", "text": text}]}
        return Message.objects.create(
            conversation=conversation,
            direction=MessageDirection.OUT,
            source=MessageSource.AGENT,
            status=status,
            error=error,
            internal=internal,
            body=body,
            idempotency_key=key or f"out:{timezone.now().timestamp()}",
        )

    return _outbound


@pytest.fixture
def url_for(tenancy: Any) -> Callable[..., str]:
    """``url_for("thread", conversation_id=...)`` for this workspace."""

    def _url(name: str, **kwargs: Any) -> str:
        return reverse(f"inbox:{name}", kwargs={"workspace_id": tenancy.workspace.pk, **kwargs})

    return _url


@pytest.fixture
def agent_client(tenancy: Any, client_for: Any) -> Any:
    """The lowest role that may reply — SPEC §4.2's `reply_in_inbox`."""
    return client_for(tenancy.user_for("agent"))


@pytest.fixture
def viewer_client(tenancy: Any, client_for: Any) -> Any:
    """Holds `use_inbox` and nothing else, so the inbox is read-only."""
    return client_for(tenancy.user_for("viewer"))
