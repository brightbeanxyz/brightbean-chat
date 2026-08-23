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
    silently reinterpreted, and ``accept_versions`` takes a *set*, so a format
    change is a rollout rather than a cutover: mint v2 while still accepting
    v1, and drop v1 once the old tokens have aged out. Tokens minted with
    ``max_age=None`` — unsubscribe links, which sit in inboxes forever — never
    age out, so for those the old version stays accepted indefinitely. A
    single-version reader would turn every one of them into a 404 the day the
    format changed, and an unsubscribe link that 404s is a compliance problem,
    not a broken link.

generic failure
    ``unsign_or_404`` turns *every* rejection — bad signature, wrong purpose,
    expired, malformed, wrong version — into a bare ``Http404`` with no detail.
    No error text, no distinguishable status codes, and constant-time
    comparison underneath (``django.core.signing`` uses
    ``constant_time_compare``), so a caller learns nothing from a failure.

**The other half: signing bodies we send.** ``sign`` mints a token *we* later
verify; :func:`sign_webhook` signs a request body a *third party* verifies
(#25's outbound webhooks, SPEC §17). Those are different jobs — a Django signed
payload is a self-contained blob only this deployment can read, which is exactly
the wrong shape for a receiver holding a shared secret — so the second one lives
here beside the first rather than in a second module inventing a second format.

Its inbound twin is ``apps.channels.security.sign_body``, which verifies a
signature a *platform* produced over a bare body. The difference is the
timestamp: an outbound delivery signs ``b"<unix>." + body`` so a captured request
cannot be replayed, whereas the inbound shapes are fixed by whichever platform
wrote them and we do not get a vote.
"""

import hashlib
import hmac
from collections.abc import Collection
from datetime import timedelta
from typing import Any

from django.core import signing
from django.http import Http404

__all__ = [
    "CURRENT_VERSION",
    "WEBHOOK_SIGNATURE_VERSION",
    "InvalidTokenError",
    "sign",
    "sign_webhook",
    "unsign",
    "unsign_or_404",
    "webhook_signature_matches",
]

CURRENT_VERSION = 1

#: Version tag on the outbound-webhook signature header value, so the scheme can
#: change without every receiver breaking on the same day.
WEBHOOK_SIGNATURE_VERSION = "v1"

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
    accept_versions: Collection[int] = (CURRENT_VERSION,),
) -> dict[str, Any]:
    """Verify ``token`` and return its payload without the version key.

    Raises ``InvalidTokenError`` on any failure. ``max_age`` (seconds or a
    ``timedelta``) bounds the token's lifetime; pass ``None`` only where the use
    case genuinely has no expiry, such as unsubscribe links.

    ``accept_versions`` lists the payload versions this call site still honours.
    During a format migration, pass every version still in circulation; the
    returned payload does not say which one matched, so a call site that needs
    to branch should read the version itself via a wider accept list and its own
    check.
    """
    try:
        data = signing.loads(token, salt=purpose, max_age=max_age)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        # SignatureExpired subclasses BadSignature, so expiry lands here too —
        # deliberately indistinguishable from every other rejection.
        raise InvalidTokenError from exc

    if not isinstance(data, dict) or data.get(_VERSION_KEY) not in set(accept_versions):
        raise InvalidTokenError

    return {key: value for key, value in data.items() if key != _VERSION_KEY}


def unsign_or_404(
    token: str,
    *,
    purpose: str,
    max_age: int | timedelta | None = None,
    accept_versions: Collection[int] = (CURRENT_VERSION,),
) -> dict[str, Any]:
    """``unsign``, but every failure becomes a bare ``Http404``.

    This is the form public views should use: a wrong token and a nonexistent
    token must be indistinguishable to the client.
    """
    try:
        return unsign(token, purpose=purpose, max_age=max_age, accept_versions=accept_versions)
    except InvalidTokenError:
        raise Http404 from None


def sign_webhook(secret: str, *, timestamp: int, raw_body: bytes) -> str:
    """Return the ``X-BrightBean-Signature`` value for one outbound delivery.

    HMAC-SHA256 over ``b"<timestamp>." + raw_body``, keyed on the endpoint's own
    secret rather than on ``SECRET_KEY``: the receiver holds that secret and has
    to be able to recompute this, which is the whole point.

    The timestamp is signed rather than merely sent, so a receiver that checks it
    against its own clock gets replay resistance for free. It is repeated in the
    ``X-BrightBean-Timestamp`` header because a receiver has to know the value in
    order to verify the digest.

    Returns ``"v1=<hex>"``. The version prefix is what lets a future scheme ship
    alongside this one rather than instead of it; receivers split on ``"="`` and
    reject a version they do not implement.
    """
    payload = str(int(timestamp)).encode("ascii") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{WEBHOOK_SIGNATURE_VERSION}={digest}"


def webhook_signature_matches(secret: str, *, timestamp: int, raw_body: bytes, presented: str) -> bool:
    """Constant-time check of a presented ``X-BrightBean-Signature`` value.

    BrightBean Chat never receives its own webhooks in production; this exists so
    the verification snippet published in ``docs/api/v1.md`` has an executable
    counterpart the test suite can hold to account. A documented verifier that is
    never run is a documented verifier that is eventually wrong.
    """
    if not secret or not presented:
        return False
    expected = sign_webhook(secret, timestamp=timestamp, raw_body=raw_body)
    return hmac.compare_digest(expected, presented)
