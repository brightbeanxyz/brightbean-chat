"""Asking a platform whether it already accepted a message (SPEC §9.4).

    Provider timeout with unknown outcome: mark status queued and schedule
    send_retry which **first checks for the provider id where the API allows
    lookup**, else retries with the same idempotency key (accepting rare
    duplicate risk on Telegram only, documented).

The lookup is a **protocol an adapter opts into**, not a method on
``apps.channels.providers.base.Adapter``. Adding it to the ABC would force every
Layer-5 adapter to implement something almost none of their platforms support,
and the honest implementations would all be ``return None``. This way an adapter
that can answer does, and ``apps.channels`` never learns the question exists.

Worth saying plainly in case a later reader expects more of this file: today no
platform on the roadmap publishes a lookup keyed on a client-supplied
idempotency key. Neither Telegram nor Meta does. So the documented ``force``
re-send is what actually happens on an unknown outcome, bounded to the genuine
unknown-outcome case by ``Message.dispatched_at``, and this seam exists because
SPEC §9.4 names it and because an adapter that *can* do better should not have
to change the retry path to do so.
"""

import logging
from typing import Any, Protocol, runtime_checkable

from apps.channels.providers.exceptions import AdapterError
from apps.channels.registry import adapter_for

logger = logging.getLogger(__name__)

__all__ = ["SupportsMessageLookup", "provider_message_id"]


@runtime_checkable
class SupportsMessageLookup(Protocol):
    """An adapter whose platform can answer "did you already take this one?"."""

    def find_sent_message(self, connection: Any, idempotency_key: str) -> str | None:
        """The provider's message id for ``idempotency_key``, or None."""
        ...


def provider_message_id(connection: Any, message: Any) -> str | None:
    """Best-effort lookup before a re-send. None means "ask again by sending".

    A failed lookup is not a failed send: the platform being unreachable for a
    read tells us nothing about the write, so it returns None and the caller
    falls through to the documented re-send.
    """
    if not message.idempotency_key:
        return None
    try:
        adapter = adapter_for(connection.platform)
    except LookupError:
        return None
    if not isinstance(adapter, SupportsMessageLookup):
        return None
    try:
        return adapter.find_sent_message(connection, message.idempotency_key) or None
    except AdapterError:
        logger.warning("Message lookup failed on %s; falling through to a re-send", connection.platform)
        return None
