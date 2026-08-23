"""What the two Meta platforms share (SPEC §§6.3, 6.4, 7.1).

Instagram (#17) and Messenger (#18) are one API wearing two hats: the same Graph
host, the same ``X-Hub-Signature-256`` scheme over the raw body, the same
``{"object": ..., "entry": [...]}`` delivery shape, the same Send API envelope
and the same OAuth error codes. This module is the half that is genuinely common,
so neither adapter has to be the place the other one reads it from.

It holds **no adapter class and no platform constant**. Every function takes the
platform or the connection it operates on, which is what keeps it importable from
either side without a cycle and what stops it becoming Messenger's module with
Instagram bolted on.

--------------------------------------------------------------------------
Why the token travels in a header
--------------------------------------------------------------------------

The Graph API accepts ``?access_token=`` and every example on the internet uses
it. This module refuses to: ``httpx`` logs the URL of every request it makes at
INFO, and ``apps.channels.providers.base.request_json`` names the *host* of a
failed call precisely because a path or query routinely carries a credential
(SECURITY-BASELINE §5). An ``Authorization: Bearer`` header keeps the token out
of the URL entirely, so there is nothing for the log scrubber to have to catch —
and the scrubber catches it anyway (``apps.common.logging`` knows both the
``Bearer`` shape and Meta's ``EAA…`` prefix), because two independent defences is
the right number for the credential that *is* the page.

--------------------------------------------------------------------------
Verification, and why it cannot happen before parsing
--------------------------------------------------------------------------

SPEC §7.1 gives Meta one webhook URL per platform per deployment, with "connection
resolved from payload ids". The signing key is the *app* secret, which is resolved
from the connection's workspace (SPEC §4's chain) — so the connection has to be
found before the signature can be checked, and finding it means reading the page
id out of an unverified body.

That ordering is inherent to the specification rather than a choice made here, and
``apps.channels.views_webhooks._ingest`` is written around it: the JSON **nesting
cap runs first**, as a byte scan that parses nothing, so the parser an
unauthenticated caller reaches is a bounded one. Nothing in this module relaxes
that; :func:`resolve_by_page_id` reads exactly one string out of the payload and
answers with a row or with None.
"""

import hashlib
import logging
import threading
from typing import TYPE_CHECKING, Any

import httpx

from apps.channels import security
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers.base import request_json
from apps.channels.providers.exceptions import APIError

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)

__all__ = [
    "GRAPH_ROOT",
    "GRAPH_VERSION",
    "MAX_PLATFORM_ID_CHARS",
    "OAUTH_ERROR_CODES",
    "SIGNATURE_HEADER",
    "TOKEN_KEY",
    "app_secret_for",
    "bounded_id",
    "entries",
    "graph_call",
    "is_reauth_error",
    "mark_needs_reauth",
    "page_token",
    "resolve_by_page_id",
    "store_page_token",
    "text_value",
    "verify_signature",
]

#: The Graph API root. A constant rather than a setting, for the reason
#: ``apps.channels.providers.telegram.API_ROOT`` gives: there is one Graph API,
#: and a configurable host that receives a page access token is an exfiltration
#: primitive rather than a feature.
GRAPH_ROOT = "https://graph.facebook.com"

#: Pinned deliberately. Meta deprecates a version roughly every two years and
#: changes payload shapes between them; an unversioned call silently follows the
#: app's default version, which an operator can change in a console we do not
#: control. Bumping this is a code change with fixtures to update.
GRAPH_VERSION = "v21.0"

#: The header Meta signs every delivery with (SPEC §§6.3, 6.4).
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: Where the page (or IG user) access token sits inside ``connection.credentials``.
TOKEN_KEY = "page_access_token"  # noqa: S105 - a dict key, not a credential

#: Meta's OAuth error codes. 190 covers an expired, revoked or invalidated token;
#: 102 is a session that can no longer be used. Both mean the same thing to an
#: operator — reconnect — and neither is fixed by retrying.
OAUTH_ERROR_CODES = frozenset({"102", "190"})

#: The width of ``platform_user_id`` and the other id columns. Longer values are
#: hashed rather than cut — see :func:`bounded_id`.
MAX_PLATFORM_ID_CHARS = 200

#: How many ``entry`` objects one delivery may carry. Meta batches, and the body
#: cap already bounds the payload, but a bound stated in events rather than bytes
#: is what keeps a legal 256 KB body from becoming thousands of parses.
MAX_ENTRIES = 100


# ---------------------------------------------------------------------------
# The Graph client
# ---------------------------------------------------------------------------


#: The process-wide connection pool. Built lazily and never closed, exactly as
#: ``telegram._POOL`` is and for the same reasons.
_POOL: httpx.Client | None = None

_POOL_LOCK = threading.Lock()


def _client() -> httpx.Client | None:
    """The HTTP client every Graph call goes through.

    A **pooled** client kept for the life of the process: ``request_json``'s
    default of one client per call means a fresh TLS handshake for every message
    sent, inside SPEC §7.1's 1.5 s inline budget, paid again for every message of
    a downgraded gallery.

    Built lazily rather than at import so a forked worker gets its own pool
    rather than inheriting sockets opened before the fork.

    **This is also the test seam.** A test monkeypatches this function to return
    an ``httpx.Client(transport=httpx.MockTransport(...))`` and the whole Meta
    stack — the real error mapping, the real 429 handling, the real payload
    building — runs without a socket. See
    ``apps/channels/tests/messenger_support.py``.
    """
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = httpx.Client(limits=httpx.Limits(max_keepalive_connections=8, max_connections=32))
    return _POOL


def graph_call(
    token: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float | None = None,
    unauthenticated: bool = False,
) -> dict[str, Any]:
    """One Graph API call. Returns the decoded body.

    Raises :class:`~apps.channels.providers.exceptions.APIError` — or
    :class:`~apps.channels.providers.exceptions.RateLimitError` on a 429 — through
    ``request_json``, which owns the timeout policy, the retry semantics and the
    "never put a URL in an error message" rule. None of that is re-implemented
    here.

    ``path`` is a Graph path *without* the version prefix (``"me/accounts"``,
    ``"1234/messages"``). It is built by the caller from constants and stored
    ids, never from user input, which is what keeps this off the SSRF guard's
    list (SECURITY-BASELINE §6): a flow author cannot change the destination.

    ``data`` sends a form-encoded body, which is what the OAuth token endpoint
    wants. Prefer it to ``params`` for anything sensitive: a query string is part
    of the URL, and ``httpx`` logs the URL of every request it makes at INFO.

    ``unauthenticated`` is for the one endpoint that has no bearer token yet —
    ``/oauth/access_token``, which authenticates with the app id and secret in its
    own body. Everything else passes a token, and an empty one is refused rather
    than sent as an anonymous call that would fail confusingly at Meta.

    ``timeout`` defaults to the inline budget SPEC §7.1 sets. Connect-time work
    passes ``BACKGROUND_TIMEOUT`` — nobody is waiting on a webhook for an OAuth
    exchange.
    """
    if not token and not unauthenticated:
        raise APIError("This connection has no access token stored.")
    headers = {} if unauthenticated else {"Authorization": f"Bearer {token}"}
    return request_json(
        method,
        f"{GRAPH_ROOT}/{GRAPH_VERSION}/{path.lstrip('/')}",
        params=params,
        json=json,
        data=data,
        # The token never enters the URL. See the module docstring.
        headers=headers,
        client=_client(),
        timeout=timeout,
    )


def is_reauth_error(exc: APIError) -> bool:
    """Whether ``exc`` means the stored credentials are dead rather than unlucky.

    Meta answers 400 with ``error.code`` 190 for an expired, revoked or
    password-changed token, and ``request_json`` has already lifted that code out
    of the body. A 401 counts too: it is what the Graph API returns when the
    ``Authorization`` header is rejected outright.
    """
    return exc.code in OAUTH_ERROR_CODES or exc.status_code == 401


def mark_needs_reauth(connection: ChannelConnection, *, platform_label: str) -> None:
    """Park a connection whose credentials the platform no longer accepts.

    Two things, in this order: the status, so nothing keeps trying and the
    settings page says why, and the notification SPEC §5 already reserved
    (``channel_needs_reauth``, registered in ``apps.notifications.events``).

    Idempotent — a page that fails ten sends in a minute must not send ten
    notifications — and best effort. It is called from an error path, so it must
    never raise on top of the failure it is describing.
    """
    if connection.status == ConnectionStatus.NEEDS_REAUTH:
        return
    try:
        connection.status = ConnectionStatus.NEEDS_REAUTH
        connection.save(update_fields=["status", "updated_at"])
    except Exception:
        logger.exception("Could not park connection %s as needing reconnection.", connection.pk)
        return

    try:
        from apps.notifications.engine import notify

        notify(
            connection.workspace,
            "channel_needs_reauth",
            context={
                # Attacker-influenced (a page names itself), so it is escaped on
                # render like every other stored display string.
                "channel_name": connection.display_name,
                "platform_label": platform_label,
            },
        )
    except Exception:
        logger.exception("Could not notify about connection %s needing reconnection.", connection.pk)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def page_token(connection: ChannelConnection) -> str:
    """The access token stored on ``connection``, or "" when there is none.

    ``credentials`` is an encrypted column, so reading it can fail on a
    deployment whose key has changed. That is a configuration problem and not
    something a webhook or a send should turn into a 500, so it reads as "no
    token" and the caller fails the operation with a named error — the treatment
    ``telegram.bot_token`` established.
    """
    try:
        credentials: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return ""
    if not isinstance(credentials, dict):
        return ""
    token = credentials.get(TOKEN_KEY)
    return token if isinstance(token, str) else ""


def store_page_token(connection: ChannelConnection, token: str, **extra: Any) -> None:
    """Put ``token`` on the connection's encrypted credentials column.

    The inverse of :func:`page_token`, and the only place a token is written.
    Both exist so the encrypted-JSON column is reached through named functions:
    ``EncryptedJSONField`` subclasses ``TextField``, so django-stubs types the
    attribute as ``str`` and every direct assignment is a type error even though
    the column holds JSON. One suppression here beats one per call site.
    """
    connection.credentials = {TOKEN_KEY: token, **extra}  # type: ignore[assignment]


def app_secret_for(connection: ChannelConnection) -> str:
    """The Meta app secret this connection's deliveries are signed with.

    SPEC §4's chain, through ``apps.credentials.resolution``: a workspace override
    beats the organization's app, which beats the deployment's environment. Meta's
    own documentation says ``app_secret`` while its OAuth endpoints say
    ``client_secret``; ``REQUIRED_CREDENTIAL_KEYS`` already accepts both spellings,
    so both are read here.

    Returns "" when nothing is configured, which
    :func:`verify_signature` turns into a refused delivery. Failing closed is the
    only option: a deployment with no app secret cannot tell a real delivery from
    a forged one, and guessing on its behalf would make the webhook endpoint
    unauthenticated.
    """
    from apps.credentials.resolution import resolve_platform_credentials

    resolution = resolve_platform_credentials(connection.platform, workspace=connection.workspace)
    credentials = resolution.credentials
    for key in ("client_secret", "app_secret"):
        value = credentials.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


def verify_signature(request: "HttpRequest", connection: ChannelConnection) -> bool:
    """Meta's ``X-Hub-Signature-256``, over the raw body, in constant time.

    Delegates the comparison to ``apps.channels.security.verify_signature_header``,
    which fails closed on a missing secret, a missing header, the wrong prefix and
    a non-hex digest — each indistinguishable from a wrong signature, because a
    distinguishable "malformed header" reply is a free oracle.

    The raw body, before any JSON parsing: re-serialising a parsed document
    changes key order and whitespace, and the digest would never match.
    """
    secret = app_secret_for(connection)
    if not secret:
        logger.warning(
            "No %s app secret is configured for workspace %s; refusing the delivery.",
            connection.platform,
            connection.workspace_id,
        )
        return False
    return security.verify_signature_header(
        secret=secret,
        raw_body=request.body,
        header_value=request.headers.get(SIGNATURE_HEADER),
    )


def resolve_by_page_id(platform: str, page_id: str) -> ChannelConnection | None:
    """The connection for one page (or IG user) id, or None.

    SPEC §5 makes ``(platform, external_id)`` unique deployment-wide, which is
    what lets one shared webhook URL address every workspace's pages without an
    id in the path.

    Crosses tenants by necessity — an inbound webhook has no session and
    therefore no workspace — so this is a deliberate, greppable ``.unscoped()``
    (CONTRIBUTING.md). It is *not* the authorisation step: whatever comes back is
    still checked by ``views_webhooks._usable`` for platform and status, and its
    signature is still verified against that row's own app secret.
    """
    page_id = page_id.strip()
    if not page_id:
        return None
    return (
        # Cross-tenant by necessity: a webhook identifies a page, not a session.
        ChannelConnection.objects.unscoped()
        .filter(platform=platform, external_id=page_id)
        .select_related("workspace")
        .first()
    )


def entries(payload: Any) -> list[dict[str, Any]]:
    """The ``entry`` list of a Meta delivery, bounded and type-checked.

    Everything here was typed by a stranger (SECURITY-BASELINE §2): a payload
    whose ``entry`` is a string, a number or absent yields an empty list rather
    than raising, and a payload carrying more entries than
    :data:`MAX_ENTRIES` is truncated with a log line rather than parsed in full.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("entry")
    if not isinstance(raw, list):
        return []
    items = [item for item in raw if isinstance(item, dict)]
    if len(items) > MAX_ENTRIES:
        logger.warning("A Meta delivery carried %s entries; parsing the first %s.", len(items), MAX_ENTRIES)
        return items[:MAX_ENTRIES]
    return items


def text_value(value: Any, limit: int) -> str:
    """A bounded string, or "". Every inbound field goes through one of these."""
    return value[:limit] if isinstance(value, str) else ""


def bounded_id(value: Any, limit: int = MAX_PLATFORM_ID_CHARS) -> str:
    """A platform id that fits its column, **hashed** rather than truncated.

    The rule this codebase already applies everywhere an attacker-controlled id
    becomes a key — ``apps.messaging.identities.bounded_key``,
    ``apps.channels.views_webhooks._dedup_id``,
    ``apps.channels.providers.telegram._private_chat_id``. Truncating narrows an
    identity key without saying so, and two ids agreeing on their first 200
    characters would become one person receiving another's conversation.

    Meta sends ids as JSON strings and occasionally as numbers; ``bool`` is
    excluded explicitly because it is an ``int`` in Python and ``"True"`` is a
    page-scoped id nobody has.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    text = str(value).replace("\x00", "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
