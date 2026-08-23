"""What Instagram and Messenger genuinely share (SPEC §6.3, §6.4, §7.1).

Issue #17 and #18 reserve this module and say whoever merges first creates it,
modelled on BrightBean Studio's ``providers/meta_messaging.py``. It landed with
#17 (L5-A), so the contract for #18 is: **add nothing that is only true of one
platform.** Two Meta platforms sharing a helper is the point; two Meta platforms
sharing a helper with an ``if platform ==`` inside it is the thing this module
exists to avoid, and would be worse than two copies.

What is genuinely shared, and why each piece is here rather than in an adapter:

``app_secret`` / ``verify_hub_signature``
    Both platforms sign the raw body with the **app** secret rather than with
    anything stored on the connection, and both resolve that secret through the
    same SPEC §4 chain. ``apps.channels.security`` already owns the constant-time
    ``sha256=`` comparison; this is the part that knows *which* secret to hand it.

``entries`` / ``changes`` / ``messaging``
    One delivery is ``{"object": ..., "entry": [{...}]}`` on both, and every
    level of that is attacker-controlled (SECURITY-BASELINE §2). Type-checking it
    once means neither adapter has to remember to.

``is_echo``
    Both platforms deliver copies of the messages *we* sent. Ingesting one files
    our own outbound message as the contact's inbound reply, which then answers
    itself. It is one field on both, and getting it wrong is expensive on both.

``bounded_text``
    Every inbound string goes through a length bound before it is carried out of
    a parse. Telegram has its own local copy because it predates this module.

Deliberately **not** here: message payload shapes. Instagram caps text at 1000
and has no file attachments; Messenger caps at 2000 and does. They look similar
enough to unify and are not, and ``apps.channels.downgrade`` already owns the
part that really is shared.
"""

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from apps.channels import security

if TYPE_CHECKING:
    from django.http import HttpRequest

    from apps.channels.models import ChannelConnection

logger = logging.getLogger(__name__)

__all__ = [
    "SIGNATURE_HEADER",
    "app_secret",
    "bounded_text",
    "changes",
    "entries",
    "is_echo",
    "messaging",
    "verify_hub_signature",
]

#: The header Meta signs every webhook delivery with.
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: Credential keys carrying the app secret. Meta's own documentation says
#: ``app_secret`` while its OAuth endpoints say ``client_secret``, and
#: ``apps.credentials.models.REQUIRED_CREDENTIAL_KEYS`` already accepts both —
#: so both are read here rather than one being declared canonical.
_SECRET_KEYS = ("app_secret", "client_secret")

#: How many entries and items one delivery may carry before the rest is dropped.
#: The body cap (``WEBHOOK_MAX_BODY_BYTES``, 256 KB) already bounds the payload,
#: but a 256 KB document of ``{}`` is tens of thousands of entries, and each one
#: costs a connection lookup. Meta batches a few dozen.
MAX_ENTRIES = 100
MAX_ITEMS_PER_ENTRY = 100


def app_secret(connection: "ChannelConnection") -> str:
    """The Meta app secret in force for ``connection``'s workspace, or "".

    Resolved through SPEC §4's chain — workspace override → organization →
    deployment env — which is the same resolution the connect flow used to
    obtain the token in the first place. Reading it per request rather than
    caching it on the connection is deliberate: an operator who rotates a
    compromised app secret in the admin expects the next delivery to be verified
    against the new one.

    Returns "" rather than raising when nothing is configured, because the caller
    turns that into a failed signature check — which is the correct outcome and
    is indistinguishable to the sender from any other refusal.
    """
    from apps.credentials.resolution import resolve_platform_credentials

    try:
        resolution = resolve_platform_credentials(connection.platform, workspace=connection.workspace)
    except Exception:
        # A decryption failure on the credential row. Nothing about it is the
        # caller's business, and it must not turn an unauthenticated request
        # into a 500 (SECURITY-BASELINE §2).
        logger.exception("Could not resolve %s app credentials for connection %s", connection.platform, connection.pk)
        return ""
    for key in _SECRET_KEYS:
        value = resolution.credentials.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def verify_hub_signature(request: "HttpRequest", connection: "ChannelConnection") -> bool:
    """``X-Hub-Signature-256`` over the **raw** body, constant time.

    Fails closed on everything — no secret configured, no header, a wrong
    prefix, a non-hex digest — and every one of those is indistinguishable to the
    caller from a wrong signature, which is what keeps the endpoint from being an
    oracle for whether an app is configured (``security.verify_signature_header``
    documents the rule).

    The raw body and not a re-serialisation of the parsed JSON: key order and
    whitespace would differ and the digest would never match. This runs before
    the endpoint parses anything for exactly that reason.
    """
    return security.verify_signature_header(
        secret=app_secret(connection),
        raw_body=request.body,
        header_value=request.headers.get(SIGNATURE_HEADER),
    )


def entries(payload: Any) -> Iterator[dict[str, Any]]:
    """The ``entry`` objects of a Meta delivery, type-checked and bounded."""
    if not isinstance(payload, dict):
        return
    raw = payload.get("entry")
    if not isinstance(raw, list):
        return
    if len(raw) > MAX_ENTRIES:
        logger.info("Meta delivery carried %s entries; parsing the first %s.", len(raw), MAX_ENTRIES)
    for item in raw[:MAX_ENTRIES]:
        if isinstance(item, dict):
            yield item


def _items(entry: Any, key: str) -> Iterator[dict[str, Any]]:
    if not isinstance(entry, dict):
        return
    raw = entry.get(key)
    if not isinstance(raw, list):
        return
    for item in raw[:MAX_ITEMS_PER_ENTRY]:
        if isinstance(item, dict):
            yield item


def messaging(entry: Any) -> Iterator[dict[str, Any]]:
    """One entry's ``messaging`` items: DMs, postbacks, referrals, deletions.

    ``standby`` is deliberately not read. It carries messages delivered while
    another app in a handover protocol holds the thread, and acting on one would
    mean replying in a conversation this deployment does not own.
    """
    yield from _items(entry, "messaging")


def changes(entry: Any) -> Iterator[dict[str, Any]]:
    """One entry's ``changes`` items: comments, mentions, and the rest."""
    yield from _items(entry, "changes")


def is_echo(item: Any) -> bool:
    """True when this messaging item is a copy of a message *we* sent.

    Meta delivers an echo of every outbound message to apps subscribed to
    ``message_echoes``, and — more importantly — an account's own DMs sent from
    the Instagram app arrive the same way. Ingesting one files our own outbound
    text as the contact's inbound reply, which then matches a keyword trigger and
    answers itself. Filtering happens on the way in rather than being left to a
    subscription setting an operator can change in a console.
    """
    if not isinstance(item, dict):
        return False
    message = item.get("message")
    return isinstance(message, dict) and bool(message.get("is_echo"))


def bounded_text(value: Any, limit: int) -> str:
    """A bounded string, or "". Every inbound field goes through this."""
    return value[:limit] if isinstance(value, str) else ""
