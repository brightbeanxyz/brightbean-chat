"""Facebook Login for Business — the credential exchange, and nothing else.

Deliberately **not** in ``apps.channels.providers.messenger``. The layer-5 ground
rules say to keep the OAuth exchange out of the adapter module, and the reason is
the same one ``apps.channels.views_telegram`` gives for being separate from the
Telegram adapter: the adapter is the thing every later platform copies, and an
OAuth dance is not part of SPEC §6.1's interface. This module is a handful of
pure-ish functions over the Graph API; ``apps.channels.views_messenger`` is the
screen an operator sees; neither knows about the other's concerns.

--------------------------------------------------------------------------
The ``state`` parameter is the whole CSRF story
--------------------------------------------------------------------------

An OAuth callback is a GET the browser makes on somebody else's say-so. Without
``state``, anyone could send a workspace admin a link that finishes *their* OAuth
flow and attaches *their* Facebook page to the victim's workspace — the classic
login-CSRF, and the reason issue #18 calls out "``state`` validated" twice.

So the state is minted with :mod:`apps.common.signing`, the project's one signer,
and carries the workspace it was minted for plus a nonce. Three properties follow
and each is tested:

* it cannot be forged — the signature is keyed on ``SECRET_KEY``;
* it cannot be replayed against another route — ``purpose`` is the signer's salt,
  so a token minted for anything else fails here;
* it cannot be used to attach a page to a workspace the *signed-in* user does not
  administer — the view checks the state's workspace against the request's, so a
  genuine state stolen from one workspace is useless in another.

It expires, too (:data:`STATE_MAX_AGE`), because an OAuth round trip takes a
minute and a token that outlives the tab it was minted in is a token somebody can
find in a proxy log later.

--------------------------------------------------------------------------
The redirect URI is fixed, and has to be
--------------------------------------------------------------------------

Meta requires every redirect URI to be whitelisted in the app's console, exactly.
A per-workspace callback path would mean whitelisting one URI per tenant, which is
not something a self-hoster can do — so the callback lives outside
``/w/<workspace_id>/``, at ``/oauth/meta/callback/``, and the workspace comes from
the signed state instead of from the path. That is also why the route carries no
tenant-shaped kwarg and is therefore not swept by ``tests/idor.py``: it has no
tenant id to fuzz, and what stands in for the sweep is the state check above.
"""

import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.urls import reverse

from apps.channels.providers import meta_common
from apps.channels.providers.base import BACKGROUND_TIMEOUT
from apps.channels.providers.exceptions import APIError
from apps.common import signing

logger = logging.getLogger(__name__)

__all__ = [
    "SCOPES",
    "STATE_MAX_AGE",
    "STATE_PURPOSE",
    "MetaPage",
    "authorize_url",
    "callback_url",
    "exchange_code",
    "list_pages",
    "mint_state",
    "read_state",
]

#: Where the login dialog lives. The Graph *API* host answers the token exchange;
#: the dialog itself is on ``www.facebook.com``, and both are constants for the
#: reason ``telegram.API_ROOT`` gives.
LOGIN_ROOT = "https://www.facebook.com"

#: Permissions requested (SPEC §6.4, issue #18).
#:
#: ``pages_messaging`` is the channel itself. ``pages_show_list`` is what makes
#: ``/me/accounts`` return anything, so the operator can pick a page.
#: ``pages_manage_metadata`` is what permits ``subscribed_apps`` and the Get
#: Started button — without it a page connects and then silently never delivers.
#: The two engagement scopes are SPEC §10's comment trigger: reading the comment
#: and posting the public reply or the like.
SCOPES: tuple[str, ...] = (
    "pages_messaging",
    "pages_show_list",
    "pages_manage_metadata",
    "pages_read_engagement",
    "pages_manage_engagement",
)

#: The signer salt. Scopes the token to this flow: one minted here cannot be
#: replayed against the unsubscribe route or the media-delivery route.
STATE_PURPOSE = "messenger-oauth"

#: How long a state stays valid. Long enough for a person to read Meta's consent
#: screen and choose pages, short enough that a token found in a log later is
#: already dead.
STATE_MAX_AGE = 15 * 60

#: Bounds on what we keep from ``/me/accounts``. Page names are chosen by whoever
#: runs the page, so they are attacker-influenced text like any other.
MAX_PAGE_NAME_CHARS = 200

#: How many pages the chooser will show. An account with more than this many
#: pages is real but rare, and an unbounded list is an unbounded page render.
MAX_PAGES = 100


@dataclass(frozen=True)
class MetaPage:
    """One page the signed-in Facebook user administers.

    ``access_token`` is the **page** token — the credential the connection ends up
    holding — so this object is never rendered, logged or put in a session. It
    exists for the seconds between ``/me/accounts`` answering and the chosen page
    being written to an encrypted column.
    """

    id: str
    name: str
    access_token: str


# ---------------------------------------------------------------------------
# The state parameter
# ---------------------------------------------------------------------------


def mint_state(workspace_id: Any) -> str:
    """A signed ``state`` for one workspace's connect attempt.

    The nonce is not decoration: without it two states minted for the same
    workspace in the same second are byte-identical, so a state captured once
    could be recognised again. With it, every attempt is a distinct token.
    """
    return signing.sign(
        {"workspace": str(workspace_id), "nonce": secrets.token_urlsafe(16)},
        purpose=STATE_PURPOSE,
    )


def read_state(state: str) -> str:
    """The workspace id a ``state`` was minted for, or "" if it is not ours.

    Every rejection — forged, expired, minted for another purpose, malformed —
    comes back as the same empty string, and the caller answers all of them
    identically. A distinguishable "expired" would tell a caller their format was
    right (:mod:`apps.common.signing` makes the same argument about its own
    exception being a single type).
    """
    try:
        payload = signing.unsign(state, purpose=STATE_PURPOSE, max_age=STATE_MAX_AGE)
    except signing.InvalidTokenError:
        return ""
    workspace = payload.get("workspace")
    return workspace if isinstance(workspace, str) else ""


# ---------------------------------------------------------------------------
# The dance
# ---------------------------------------------------------------------------


def callback_url() -> str:
    """The absolute URL Meta redirects back to. Must match the app's whitelist.

    Built from ``APP_URL`` — the deployment's own configured address — rather than
    from the request, following ``telegram.webhook_url`` and
    ``apps.media_library.delivery``: the value has to be identical in the
    authorize call and in the token exchange, and behind a proxy
    ``request.build_absolute_uri`` is not the public address.
    """
    return urljoin(settings.APP_URL.rstrip("/") + "/", reverse("messenger_oauth_callback").lstrip("/"))


def authorize_url(*, client_id: str, state: str) -> str:
    """Where to send the operator's browser to start Facebook Login for Business.

    ``auth_type=rerequest`` so an operator who declined a permission the first
    time is asked again rather than being handed straight back with the same
    partial grant and no explanation.
    """
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url(),
            "state": state,
            "scope": ",".join(SCOPES),
            "response_type": "code",
            "auth_type": "rerequest",
        }
    )
    return f"{LOGIN_ROOT}/{meta_common.GRAPH_VERSION}/dialog/oauth?{query}"


def exchange_code(*, code: str, client_id: str, client_secret: str) -> str:
    """Trade the callback's ``code`` for a long-lived user access token.

    Two calls, because Meta's short-lived token expires in about an hour and the
    page tokens derived from it inherit that lifetime. Exchanging for a long-lived
    user token first is what makes the resulting **page** token effectively
    permanent, which is the property a channel needs — SPEC §6.4 stores a page
    token and expects it to keep working.

    **POSTed, with a form body.** Every example in Meta's documentation puts the
    app secret and the code in a query string, and this deliberately does not:
    ``httpx`` logs the URL of every request it makes at INFO, so a GET here would
    write a live app secret and a single-use authorization code into the
    application log of every deployment (SECURITY-BASELINE §5). The token endpoint
    accepts a form body, which never enters the URL at all.

    ``apps.common.logging`` would scrub the ``client_secret=`` shape anyway; it
    would **not** have scrubbed ``code=``, which is why this is a body rather
    than a scrubber rule. ``test_messenger_connect.py`` holds both halves.

    Raises :class:`~apps.channels.providers.exceptions.APIError`, whose message
    carries no detail the caller could turn into an oracle.
    """
    short_lived = _oauth_token(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback_url(),
            "code": code,
        }
    )
    return _oauth_token(
        {
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": short_lived,
        }
    )


def _oauth_token(form: dict[str, str]) -> str:
    body = meta_common.graph_call(
        # The one Graph endpoint with no bearer token to present: it authenticates
        # with the app id and secret in its own body.
        "",
        "POST",
        "oauth/access_token",
        data=form,
        timeout=BACKGROUND_TIMEOUT,
        unauthenticated=True,
    )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise APIError("Facebook returned no access token")
    return token


def list_pages(user_token: str) -> list[MetaPage]:
    """The pages this person administers, with each page's own access token.

    ``/me/accounts`` is the only way to get a page token, and it needs
    ``pages_show_list``. An account with no pages answers with an empty list,
    which the view renders as an explanation rather than an error — "you have no
    Facebook pages" is a true and actionable thing to be told.
    """
    body = meta_common.graph_call(
        user_token,
        "GET",
        "me/accounts",
        params={"fields": "id,name,access_token", "limit": str(MAX_PAGES)},
        timeout=BACKGROUND_TIMEOUT,
    )
    rows = body.get("data")
    if not isinstance(rows, list):
        return []

    pages: list[MetaPage] = []
    for item in rows[:MAX_PAGES]:
        if not isinstance(item, dict):
            continue
        page_id = meta_common.bounded_id(item.get("id"))
        token = item.get("access_token")
        if not page_id or not isinstance(token, str) or not token:
            # A page the app cannot message is a page we cannot connect. Listing
            # it would offer the operator a choice that fails after they make it.
            continue
        name = item.get("name")
        pages.append(
            MetaPage(
                id=page_id,
                name=(name.strip()[:MAX_PAGE_NAME_CHARS] if isinstance(name, str) else "") or page_id,
                access_token=token,
            )
        )
    return pages
