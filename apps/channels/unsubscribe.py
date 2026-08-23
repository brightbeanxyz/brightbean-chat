"""Minting and reading the ``/u/<token>`` unsubscribe link (SPEC §6.7).

``apps/common/signing.py`` names this module's route in its own docstring as the
canonical case, and two of the things it insists on are load-bearing here:

**``max_age=None``.** An unsubscribe link sits in an inbox forever. Somebody
finding a two-year-old newsletter and clicking "unsubscribe" must be
unsubscribed, not shown a 404 — an expired unsubscribe link is a compliance
problem rather than a broken link.

**The old payload version stays accepted.** Because the tokens never age out,
there is no date after which the v1 readers can be removed. :data:`ACCEPTED_VERSIONS`
is a tuple that only ever grows; minting moves to the newest version while every
older one keeps resolving. A cutover here would turn every link already in an
inbox into a 404 on the day of the deploy.

**The payload carries the mailbox, and that is a deliberate reversal.** v1 held
the identity id alone, on the reasoning that a signed-not-encrypted token should
not carry an address a forwarded link would leak. That reasoning was right about
the leak and wrong about the priority: ``ContactChannelIdentity.channel_connection``
cascades, so disconnecting an email channel deletes every identity on it and
every unsubscribe link already sitting in an inbox started answering 404 — the
exact failure the ``max_age=None`` rule above exists to prevent, arriving by a
different door.

So v2 carries ``{workspace, address}`` as well, and the view falls back to them
when the identity is gone. What that gives up is small and already true
elsewhere: the link is delivered *to* that mailbox, in a message whose ``To``
header is that mailbox, so anyone positioned to read the URL has the address
already. What it buys is that the promise this module makes — an unsubscribe
link works forever — survives an operator reconnecting a channel.

v1 tokens stay accepted, and always will: they never expire, so there is no date
after which the old reader can be removed.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from apps.common.addresses import normalize_email
from apps.common.signing import sign, unsign_or_404

__all__ = [
    "ACCEPTED_VERSIONS",
    "ADDRESS_KEY",
    "IDENTITY_KEY",
    "MINT_VERSION",
    "PURPOSE",
    "WORKSPACE_KEY",
    "Target",
    "mint_token",
    "target_from_token",
    "unsubscribe_url",
]

#: The signer salt. A token minted for unsubscribe cannot be replayed against
#: ``/internal/tick`` or a media-delivery URL even though all three are signed
#: with the same ``SECRET_KEY``.
PURPOSE = "unsubscribe"

#: Short keys, because this string is embedded in every outbound email and the
#: signer base64s the whole payload.
IDENTITY_KEY = "i"
WORKSPACE_KEY = "w"
ADDRESS_KEY = "a"

#: The version new links are minted at.
MINT_VERSION = 2

#: Every version still in circulation. **Additive only** — see the module
#: docstring. These tokens never expire, so no version is ever removed.
ACCEPTED_VERSIONS: tuple[int, ...] = (1, 2)


@dataclass(frozen=True)
class Target:
    """Who a token names: an identity, and the mailbox behind it.

    ``identity_id`` is empty for nothing; ``workspace_id`` and ``address`` are
    empty on a v1 token, which carried the identity alone.
    """

    identity_id: str
    workspace_id: str = ""
    address: str = ""


def mint_token(identity: Any) -> str:
    """A signed token naming ``identity`` and its mailbox. Never expires."""
    return sign(
        {
            IDENTITY_KEY: str(identity.pk),
            WORKSPACE_KEY: str(getattr(identity, "workspace_id", "") or ""),
            ADDRESS_KEY: str(getattr(identity, "platform_user_id", "") or ""),
        },
        purpose=PURPOSE,
        version=MINT_VERSION,
    )


def unsubscribe_url(identity: Any) -> str:
    """The absolute ``/u/<token>/`` URL to put in an email.

    Built from ``settings.APP_URL`` rather than anything read off a request,
    because the send path runs in a worker where there is no request — the same
    reasoning ``apps.media_library.delivery`` gives for its own links.
    """
    path = reverse("unsubscribe", kwargs={"token": mint_token(identity)})
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


def target_from_token(token: str) -> Target:
    """What a token names, or ``Http404`` for every failure.

    ``max_age=None`` and the full :data:`ACCEPTED_VERSIONS` set, per the module
    docstring. Every rejection — bad signature, wrong purpose, unknown version,
    malformed — comes back as one indistinguishable bare 404.

    A v1 token yields a :class:`Target` with only ``identity_id`` set; the view
    handles the missing halves rather than this function guessing at them.
    """
    payload = unsign_or_404(token, purpose=PURPOSE, max_age=None, accept_versions=ACCEPTED_VERSIONS)
    return Target(
        identity_id=str(payload.get(IDENTITY_KEY) or ""),
        workspace_id=str(payload.get(WORKSPACE_KEY) or ""),
        address=normalize_email(str(payload.get(ADDRESS_KEY) or "")),
    )
