"""Instagram's OAuth dance and token lifecycle (SPEC §6.3, SECURITY-BASELINE §§4, 5).

Deliberately **not** in ``apps.channels.providers.instagram``. The layer-5 brief
is explicit — "keep the credential exchange out of the adapter module proper" —
and the reason is the same one that keeps ``views_telegram`` out of
``providers/telegram``: the adapter is the thing five more platforms copy, and
an OAuth round trip is not part of what they are copying. What lives here is
everything that mints, stores, refreshes or invalidates the credential; the
adapter imports :func:`access_token` and nothing else.

--------------------------------------------------------------------------
The ``state`` parameter is a CSRF token, so it is a signed one
--------------------------------------------------------------------------

Without it, anyone who can make an operator's browser hit the callback with a
code of *their* choosing connects **their** Instagram account into the
operator's workspace — every DM to that account then lands in a stranger's
inbox. So ``state`` is minted with :mod:`apps.common.signing`, which
SECURITY-BASELINE §4 makes the one implementation for token-bearing routes, and
it carries the workspace *and* the user it was minted for. The callback checks
both: a state signed for another workspace, another user, or more than
``STATE_MAX_AGE`` ago is refused before a single byte is exchanged with Meta.

``purpose`` is the signer salt, so a state token cannot be replayed against the
unsubscribe route or the tick endpoint even though all three share ``SECRET_KEY``.

--------------------------------------------------------------------------
Three tokens, one stored
--------------------------------------------------------------------------

Meta hands out a short-lived token (1 hour) which is exchanged for a long-lived
one (60 days) which is refreshed for another 60. Only the long-lived token is
ever written to ``connection.credentials``; the short-lived one exists for the
two seconds between the two calls. ``token_expires_at`` is stored beside it so
:func:`refresh_expiring_tokens` can find the connections that need attention
without asking Meta about every account every hour.

A refresh that fails is not a transient error to swallow: the account stops
working in at most 60 days and nobody finds out. It flips the connection to
``needs_reauth`` and raises the ``channel_needs_reauth`` notification, which is
the state the reconnect button clears.

Nothing here logs a token, and nothing renders one. ``request_json`` reports the
host of a failed call and never the path, which matters more than usual on this
module: Meta's token endpoints take credentials in the query string.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.channels.providers.base import BACKGROUND_TIMEOUT, request_json
from apps.channels.providers.exceptions import APIError
from apps.common import signing
from apps.common.platforms import Platform
from apps.queueing.housekeeping import register_housekeeping_job

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "SCOPES",
    "STATE_MAX_AGE",
    "STATE_PURPOSE",
    "InstagramCredentialsMissingError",
    "access_token",
    "account_profile",
    "app_credentials",
    "authorize_url",
    "callback_url",
    "exchange_code",
    "exchange_for_long_lived",
    "mark_needs_reauth",
    "read_state",
    "refresh_expiring_tokens",
    "refresh_long_lived",
    "sign_state",
    "store_credentials",
    "token_expires_at",
]

#: Where the operator authorises. Instagram Login's own host, not Facebook's —
#: this is the "Instagram API with Instagram Login" product (SPEC §6.3), and the
#: Facebook-login-based variant is explicitly out of scope in issue #17.
AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"

#: Where a code becomes a short-lived token. Constants rather than settings: a
#: configurable host on an endpoint that receives the app secret is an
#: exfiltration primitive rather than a feature.
TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - an endpoint, not a credential

#: The Graph host, which serves the long-lived exchange and the refresh.
GRAPH_ROOT = "https://graph.instagram.com"

#: SPEC §6.3's three scopes, exactly. ``manage_comments`` is what the
#: comment-to-DM trigger needs; without it the connect still succeeds and every
#: public reply fails, so it is requested up front rather than incrementally.
SCOPES: tuple[str, ...] = (
    "instagram_business_basic",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
)

#: The signer salt for the ``state`` parameter. See the module docstring.
STATE_PURPOSE = "instagram-oauth"

#: How long a ``state`` stays valid. An OAuth round trip is seconds; ten minutes
#: covers an operator who reads Meta's permission screen carefully, and is far
#: short of anything worth capturing and replaying.
STATE_MAX_AGE = 600

#: Where the long-lived token and its expiry live inside ``credentials``.
TOKEN_KEY = "access_token"  # noqa: S105 - a dict key, not a credential
EXPIRES_KEY = "token_expires_at"  # noqa: S105 - a dict key, not a credential
USER_ID_KEY = "user_id"

#: Refresh this far before expiry. Meta's long-lived token lasts 60 days and can
#: only be refreshed while it is still valid, so the margin has to be wider than
#: the longest plausible outage of the hourly housekeeping sweep.
REFRESH_MARGIN = timedelta(days=7)

#: A long-lived token Meta gave us no ``expires_in`` for. Meta documents 60 days;
#: assuming *less* is the safe direction, because the cost of refreshing early is
#: one API call and the cost of refreshing late is a dead channel.
DEFAULT_TOKEN_LIFETIME = timedelta(days=50)


class InstagramCredentialsMissingError(Exception):
    """This workspace has no Instagram app credentials configured (SPEC §4)."""


def _client() -> "httpx.Client | None":
    """The HTTP client the token exchanges go through. **This is the test seam.**

    None means ``request_json`` builds and closes one per call, which is right
    here: unlike the adapter's send path, every call in this module happens once
    per connect or once per token per two months, so a pooled connection would
    idle for weeks to save one handshake.

    It exists as a function anyway so a test can monkeypatch it to an
    ``httpx.Client`` on a ``MockTransport`` — and then the **real** error mapping
    runs, which is the difference between testing this module and testing a stub
    of it. Same seam, same reason, as ``providers.instagram._client``.
    """
    return None


# ---------------------------------------------------------------------------
# App credentials and the callback URL
# ---------------------------------------------------------------------------


def app_credentials(workspace: Any) -> tuple[str, str]:
    """``(client_id, client_secret)`` for ``workspace``, through SPEC §4's chain.

    Both spellings Meta uses are accepted — its documentation says
    ``app_id``/``app_secret`` while its OAuth endpoints say
    ``client_id``/``client_secret``, and ``apps.credentials.models`` already
    treats them as aliases.

    Raises rather than returning blanks: every caller is about to build a URL or
    a request out of these, and an empty client id produces an opaque failure at
    Meta instead of a sentence an operator can act on.
    """
    from apps.credentials.resolution import resolve_platform_credentials

    resolution = resolve_platform_credentials(Platform.INSTAGRAM.value, workspace=workspace)
    client_id = _first(resolution.credentials, "client_id", "app_id")
    client_secret = _first(resolution.credentials, "client_secret", "app_secret")
    if not client_id or not client_secret:
        raise InstagramCredentialsMissingError(
            "This workspace has no Instagram app credentials. Add them under "
            "Settings -> Credentials, or set PLATFORM_INSTAGRAM_CLIENT_ID and "
            "PLATFORM_INSTAGRAM_CLIENT_SECRET on the deployment."
        )
    return client_id, client_secret


def _first(credentials: Any, *keys: str) -> str:
    values = credentials if isinstance(credentials, dict) else {}
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def callback_url() -> str:
    """The one redirect URI this deployment registers with Meta.

    ``APP_URL`` rather than ``request.build_absolute_uri``, following
    ``providers.telegram.webhook_url``: Meta matches the redirect URI against its
    app configuration **exactly**, so a deployment behind a proxy must send the
    address the operator registered rather than whatever the proxy reported.

    It carries no workspace id, and cannot: one app has one registered redirect
    URI, and a per-workspace URL would mean registering a new one per tenant.
    The workspace travels in the signed ``state`` instead.
    """
    path = reverse("instagram_callback")
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def sign_state(*, workspace_id: Any, user_id: Any) -> str:
    """A signed ``state`` binding this flow to one workspace and one operator."""
    return signing.sign({"ws": str(workspace_id), "u": str(user_id)}, purpose=STATE_PURPOSE)


def read_state(raw: str) -> dict[str, str] | None:
    """The payload of a valid ``state``, or ``None`` for any invalid one.

    One return value for every rejection — tampered, expired, minted for another
    purpose, malformed — because the caller does the same thing with all of them
    and a distinguishable answer is an oracle.
    """
    if not raw:
        return None
    try:
        payload = signing.unsign(raw, purpose=STATE_PURPOSE, max_age=STATE_MAX_AGE)
    except signing.InvalidTokenError:
        return None
    workspace_id = payload.get("ws")
    user_id = payload.get("u")
    if not isinstance(workspace_id, str) or not isinstance(user_id, str):
        return None
    return {"ws": workspace_id, "u": user_id}


def authorize_url(*, client_id: str, state: str) -> str:
    """Where to send the operator's browser to authorise this app."""
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url(),
            "response_type": "code",
            "scope": ",".join(SCOPES),
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


# ---------------------------------------------------------------------------
# The token exchanges
# ---------------------------------------------------------------------------


def exchange_code(*, code: str, client_id: str, client_secret: str) -> tuple[str, str]:
    """A one-hour token and the account's id, from an authorisation code.

    Form-encoded and POSTed, which is the only shape this endpoint accepts —
    and it keeps the app secret out of a URL, which is where a proxy access log
    would keep it forever.
    """
    body = request_json(
        "POST",
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": callback_url(),
            "code": code,
        },
        timeout=BACKGROUND_TIMEOUT,
        client=_client(),
    )
    token = body.get("access_token")
    user_id = body.get("user_id")
    if not isinstance(token, str) or not token:
        raise APIError("Instagram returned no access token for that code")
    return token, str(user_id) if user_id is not None else ""


def exchange_for_long_lived(*, token: str, client_secret: str) -> tuple[str, datetime]:
    """Trade a short-lived token for the 60-day one, with its expiry."""
    body = request_json(
        "GET",
        f"{GRAPH_ROOT}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": token,
        },
        timeout=BACKGROUND_TIMEOUT,
        client=_client(),
    )
    return _token_and_expiry(body)


def refresh_long_lived(token: str) -> tuple[str, datetime]:
    """Extend a long-lived token by another 60 days.

    Only works while the token is still valid and at least 24 hours old, which
    is why :func:`refresh_expiring_tokens` runs on a margin rather than on the
    last day.
    """
    body = request_json(
        "GET",
        f"{GRAPH_ROOT}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
        timeout=BACKGROUND_TIMEOUT,
        client=_client(),
    )
    return _token_and_expiry(body)


def _token_and_expiry(body: dict[str, Any]) -> tuple[str, datetime]:
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise APIError("Instagram returned no long-lived access token")
    expires_in = body.get("expires_in")
    lifetime = DEFAULT_TOKEN_LIFETIME
    if isinstance(expires_in, int) and not isinstance(expires_in, bool) and 0 < expires_in < 60 * 60 * 24 * 365:
        lifetime = timedelta(seconds=expires_in)
    return token, timezone.now() + lifetime


def account_profile(token: str) -> dict[str, str]:
    """``{"user_id": ..., "username": ...}`` for the account this token belongs to.

    ``user_id`` is the Instagram professional account id, which is what arrives
    as ``entry[].id`` on every webhook delivery — so it is the value the
    connection's ``external_id`` has to hold for
    ``InstagramAdapter.resolve_connection`` to find the row. ``id`` is the
    app-scoped id and is only a fallback.
    """
    body = request_json(
        "GET",
        f"{GRAPH_ROOT}/me",
        params={"fields": "user_id,username", "access_token": token},
        timeout=BACKGROUND_TIMEOUT,
        client=_client(),
    )
    user_id = body.get("user_id") or body.get("id")
    username = body.get("username")
    if user_id is None or not isinstance(username, str) or not username:
        raise APIError("Instagram returned an unusable account profile")
    return {"user_id": str(user_id)[:200], "username": username[:200]}


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------


def access_token(connection: Any) -> str:
    """The stored long-lived token, or "" when there is none.

    ``credentials`` is an encrypted column, so reading it can fail on a
    deployment whose key has changed. That is a configuration problem and not
    something a webhook or a send should turn into a 500, so it reads as "no
    token" and the caller fails the operation with a named error — the same
    contract ``providers.telegram.bot_token`` documents.
    """
    token = _credentials(connection).get(TOKEN_KEY)
    return token if isinstance(token, str) else ""


def token_expires_at(connection: Any) -> datetime | None:
    """When the stored token stops working, or ``None`` if we were never told."""
    from django.utils.dateparse import parse_datetime

    raw = _credentials(connection).get(EXPIRES_KEY)
    if not isinstance(raw, str):
        return None
    try:
        parsed = parse_datetime(raw)
    except ValueError:
        # parse_datetime raises on a well-formed string with impossible values
        # ("2026-02-30T…"). The column is ours, but a restored backup or a hand
        # edit is not something an hourly sweep should crash on.
        return None
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else parsed.replace(tzinfo=UTC)


def store_credentials(connection: Any, *, token: str, expires_at: datetime, user_id: str = "") -> None:
    """Write the token and its expiry. The only place either is written.

    Replaces rather than merges: a reconnect issues a brand-new token, and
    carrying a stale key across would leave a value nothing sets and nothing
    clears. Named functions on both sides so the encrypted-JSON column is never
    reached by raw attribute access — ``EncryptedJSONField`` subclasses
    ``TextField``, so django-stubs types the attribute as ``str`` and a direct
    assignment is a type error at every call site rather than one here.
    """
    payload: dict[str, Any] = {TOKEN_KEY: token, EXPIRES_KEY: expires_at.isoformat()}
    if user_id:
        payload[USER_ID_KEY] = user_id
    connection.credentials = payload  # type: ignore[assignment]


def _credentials(connection: Any) -> dict[str, Any]:
    try:
        credentials: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return {}
    return credentials if isinstance(credentials, dict) else {}


# ---------------------------------------------------------------------------
# Reauth
# ---------------------------------------------------------------------------


def mark_needs_reauth(connection: Any) -> None:
    """Flag a connection whose credentials the platform no longer accepts.

    Idempotent, and it notifies **only on the transition**: a dead token is
    retried by every send and every hourly sweep, and one notification per
    attempt is how an operator learns to ignore the bell.

    The status write is narrowed to a row still marked active, so a connection an
    operator disabled in the meantime is not quietly switched back to a state
    that reads as "connected but broken".
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus
    from apps.notifications.engine import notify

    updated = (
        ChannelConnection.objects.for_workspace(connection.workspace_id)
        .filter(pk=connection.pk, status=ConnectionStatus.ACTIVE)
        .update(status=ConnectionStatus.NEEDS_REAUTH, updated_at=timezone.now())
    )
    if not updated:
        return
    connection.status = ConnectionStatus.NEEDS_REAUTH
    logger.warning("Instagram connection %s needs reconnecting.", connection.pk)
    try:
        notify(
            connection.workspace,
            "channel_needs_reauth",
            context={
                # The display name is operator-authored, not platform-supplied,
                # and the notification template escapes it either way.
                "channel_name": connection.display_name or "An Instagram account",
                "platform_label": "Instagram",
            },
        )
    except Exception:
        # The status is the thing that matters; a notification failure must not
        # leave the connection looking healthy.
        logger.exception("Could not notify about connection %s needing reauth.", connection.pk)


# ---------------------------------------------------------------------------
# The refresh sweep
# ---------------------------------------------------------------------------


@register_housekeeping_job("refresh_instagram_tokens")
def _refresh_instagram_tokens_job() -> str | None:
    """The zero-argument, string-returning shape the hourly sweep expects.

    A thin wrapper rather than decorating :func:`refresh_expiring_tokens`
    directly, the same call ``apps.channels.housekeeping`` makes for its own
    jobs: that one takes a margin and returns a count, which is what the tests
    want, and the sweep wants neither. Returning None when nothing needed
    refreshing keeps the hourly log quiet.
    """
    refreshed = refresh_expiring_tokens()
    return f"refreshed {refreshed} Instagram token(s)" if refreshed else None


def refresh_expiring_tokens(margin: timedelta | None = None) -> int:
    """Refresh every Instagram token inside :data:`REFRESH_MARGIN` of expiry.

    A housekeeping sweep rather than a queue row scheduled at connect time, and
    the difference is recovery: a queue row that fails five times is gone, and
    the connection then dies silently 60 days later. A sweep re-examines the
    world every hour, so a failure that was really an outage repairs itself and
    one that was really a revoked token is reported once, by
    :func:`mark_needs_reauth`.

    Cross-tenant by necessity — an hourly sweep has no session workspace — so
    the ``.unscoped()`` is deliberate and greppable (CONTRIBUTING.md).
    """
    from apps.channels.models import ChannelConnection, ConnectionStatus

    cutoff = timezone.now() + (margin if margin is not None else REFRESH_MARGIN)
    refreshed = 0
    # Cross-tenant by necessity: the housekeeping sweep runs for the whole
    # deployment and has no workspace to scope by.
    connections = (
        ChannelConnection.objects.unscoped()
        .filter(platform=Platform.INSTAGRAM.value, status=ConnectionStatus.ACTIVE)
        .select_related("workspace")
        .order_by("created_at")
    )
    for connection in connections:
        expires = token_expires_at(connection)
        if expires is not None and expires > cutoff:
            continue
        if _refresh_one(connection):
            refreshed += 1
    return refreshed


def _refresh_one(connection: Any) -> bool:
    token = access_token(connection)
    if not token:
        # A connection with no token can never send. Nothing to refresh and
        # nothing a retry would fix, so it is reported the same way a rejected
        # refresh is rather than being swept past every hour in silence.
        mark_needs_reauth(connection)
        return False
    try:
        fresh, expires_at = refresh_long_lived(token)
    except APIError:
        # No exception detail in the log: this code path is one of the few
        # places a live token exists in plain text (SECURITY-BASELINE §5).
        logger.info("Instagram token refresh was rejected for connection %s.", connection.pk)
        mark_needs_reauth(connection)
        return False
    except Exception:
        logger.exception("Instagram token refresh failed for connection %s.", connection.pk)
        return False

    store_credentials(connection, token=fresh, expires_at=expires_at, user_id=connection.external_id)
    connection.save(update_fields=["credentials", "updated_at"])
    logger.info("Refreshed the Instagram token on connection %s.", connection.pk)
    return True
