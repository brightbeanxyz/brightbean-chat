"""The one signing utility for public token routes (SECURITY-BASELINE §4).

**Contract for every later issue.** Every unauthenticated, token-bearing route
in BrightBean Chat uses this module and nothing else:

* ``/u/`` unsubscribe links (#21)
* ``/c/`` click-tracking redirects and ``/o/`` open pixels (#26)
* flow-preview links (#10)
* ``/internal/tick`` (#5)

Do not reach for ``django.core.signing`` directly, and do not invent a second
token format. Uniformity is the point: one implementation means one place to
audit, one place to rotate, and one place where the failure mode is fixed.

Three properties the wrappers add over raw ``django.core.signing``:

``purpose``
    Becomes the signer salt, so a token minted for unsubscribe cannot be
    replayed against the tick endpoint even though both are signed with the
    same ``SECRET_KEY``.

versioning
    Every payload carries ``"v"``. An unknown version is rejected rather than
    silently reinterpreted, which is what makes a future format change safe.

generic failure
    ``unsign_or_404`` turns *every* rejection — bad signature, wrong purpose,
    expired, malformed, wrong version — into a bare ``Http404`` with no detail.
    No error text, no distinguishable status codes, and constant-time
    comparison underneath (``django.core.signing`` uses
    ``constant_time_compare``), so a caller learns nothing from a failure.
"""

from datetime import timedelta
from typing import Any

from django.core import signing
from django.http import Http404

__all__ = [
    "CURRENT_VERSION",
    "InvalidTokenError",
    "sign",
    "unsign",
    "unsign_or_404",
]

CURRENT_VERSION = 1

_VERSION_KEY = "v"


class InvalidTokenError(Exception):
    """Raised for any token that fails verification, for any reason.

    Deliberately one exception type: callers must not be able to branch on
    *why* a token was rejected, because neither must attackers.
    """


def sign(payload: dict[str, Any], *, purpose: str, version: int = CURRENT_VERSION) -> str:
    """Return a signed, URL-safe token carrying ``payload``.

    ``purpose`` scopes the token — use a stable, descriptive string such as
    ``"unsubscribe"`` or ``"flow-preview"``. Payloads must be JSON-serialisable
    and must not contain a ``"v"`` key.

    The payload is signed, **not encrypted**: anyone holding the token can read
    it. Never put a credential in one.
    """
    if _VERSION_KEY in payload:
        raise ValueError(f"{_VERSION_KEY!r} is reserved for the payload version.")
    return signing.dumps({_VERSION_KEY: version, **payload}, salt=purpose, compress=True)


def unsign(
    token: str,
    *,
    purpose: str,
    max_age: int | timedelta | None = None,
    version: int = CURRENT_VERSION,
) -> dict[str, Any]:
    """Verify ``token`` and return its payload without the version key.

    Raises ``InvalidTokenError`` on any failure. ``max_age`` (seconds or a
    ``timedelta``) bounds the token's lifetime; pass ``None`` only where the use
    case genuinely has no expiry, such as unsubscribe links.
    """
    try:
        data = signing.loads(token, salt=purpose, max_age=max_age)
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError) as exc:
        raise InvalidTokenError from exc

    if not isinstance(data, dict) or data.get(_VERSION_KEY) != version:
        raise InvalidTokenError

    return {key: value for key, value in data.items() if key != _VERSION_KEY}


def unsign_or_404(
    token: str,
    *,
    purpose: str,
    max_age: int | timedelta | None = None,
    version: int = CURRENT_VERSION,
) -> dict[str, Any]:
    """``unsign``, but every failure becomes a bare ``Http404``.

    This is the form public views should use: a wrong token and a nonexistent
    token must be indistinguishable to the client.
    """
    try:
        return unsign(token, purpose=purpose, max_age=max_age, version=version)
    except InvalidTokenError:
        raise Http404 from None
