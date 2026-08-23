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

The payload is the identity id and nothing else. It is **signed, not
encrypted** — anyone holding the link can read what is in it — so the address
itself deliberately stays out of it: an opaque UUID in a URL that gets forwarded,
logged by a mail gateway or pasted into a support ticket leaks nothing, while the
mailbox would. The address is looked up from the identity at click time.
"""

from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from apps.common.signing import CURRENT_VERSION, sign, unsign_or_404

__all__ = [
    "ACCEPTED_VERSIONS",
    "IDENTITY_KEY",
    "MINT_VERSION",
    "PURPOSE",
    "identity_id_from_token",
    "mint_token",
    "unsubscribe_url",
]

#: The signer salt. A token minted for unsubscribe cannot be replayed against
#: ``/internal/tick`` or a media-delivery URL even though all three are signed
#: with the same ``SECRET_KEY``.
PURPOSE = "unsubscribe"

#: Short key, because this string is embedded in every outbound email and the
#: signer base64s the whole payload.
IDENTITY_KEY = "i"

#: The version new links are minted at.
MINT_VERSION = CURRENT_VERSION

#: Every version still in circulation. **Additive only** — see the module
#: docstring. Adding a v2 means appending 2 here and moving :data:`MINT_VERSION`,
#: never replacing this tuple.
ACCEPTED_VERSIONS: tuple[int, ...] = (1,)


def mint_token(identity: Any) -> str:
    """A signed token naming ``identity``. Never expires."""
    return sign({IDENTITY_KEY: str(identity.pk)}, purpose=PURPOSE, version=MINT_VERSION)


def unsubscribe_url(identity: Any) -> str:
    """The absolute ``/u/<token>/`` URL to put in an email.

    Built from ``settings.APP_URL`` rather than anything read off a request,
    because the send path runs in a worker where there is no request — the same
    reasoning ``apps.media_library.delivery`` gives for its own links.
    """
    path = reverse("unsubscribe", kwargs={"token": mint_token(identity)})
    return urljoin(settings.APP_URL.rstrip("/") + "/", path.lstrip("/"))


def identity_id_from_token(token: str) -> str:
    """The identity id a token names, or ``Http404`` for every failure.

    ``max_age=None`` and the full :data:`ACCEPTED_VERSIONS` set, per the module
    docstring. Every rejection — bad signature, wrong purpose, unknown version,
    malformed — comes back as one indistinguishable bare 404.
    """
    payload = unsign_or_404(token, purpose=PURPOSE, max_age=None, accept_versions=ACCEPTED_VERSIONS)
    return str(payload.get(IDENTITY_KEY) or "")
