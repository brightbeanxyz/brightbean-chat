"""Minting and verifying public API keys (SPEC §5 ``api_key``, §17).

The token an integration presents is never stored. What the row holds is a
keyed digest of it plus an indexed, non-secret prefix — CONTRIBUTING.md's rule
for "a credential that arrives in a request", and the same shape
``apps.members.models.Invitation`` uses for invite tokens.

Token format::

    bb_<secret>_<lookup>

``secret``
    43 URL-safe characters from ``secrets.token_urlsafe(32)`` — 256 bits. This
    is the only secret material; everything else in the token is derived from
    it and is safe to store and display.

``lookup``
    The first 8 hex characters of ``sha256(secret)``. Stored in plaintext and
    indexed, so verification is one index hit rather than a table scan of HMACs.
    It is also the handle the settings UI shows for a key whose plaintext is
    long gone.

Why both a lookup prefix and a digest, when a unique index on the digest alone
would find the row: the prefix is what keeps the *comparison* in Python, where
it is ``secrets.compare_digest``. It is deliberately **not** unique — 8 hex
characters is 32 bits, so a birthday collision arrives somewhere around 65k
keys, and a unique index would turn that into a random issuance failure. The
digest carries the uniqueness constraint; the prefix carries the lookup.

The digest is ``apps.common.encryption.hmac_digest``, keyed from ``SECRET_KEY``
rather than a bare hash: an unkeyed digest of a 256-bit token is not guessable,
but keying costs nothing and means a database dump is useless on its own.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

from apps.common.encryption import hmac_digest

__all__ = [
    "LOOKUP_LENGTH",
    "TOKEN_PREFIX",
    "MintedKey",
    "digest_for",
    "lookup_for",
    "mint",
    "parse",
]

#: Product-branded (SPEC §22: this repo ships as BrightBean Chat). SPEC §17
#: wrote ``oc_`` while "OpenChat" was still the working title; the header names
#: and this prefix were rebranded together so integrators copy one vocabulary.
TOKEN_PREFIX = "bb_"  # noqa: S105 - a prefix, not a credential

LOOKUP_LENGTH = 8

_SECRET_BYTES = 32

# ``token_urlsafe`` emits [A-Za-z0-9_-]; the lookup is lowercase hex. Anchored
# and length-bounded so a hostile Authorization header cannot make this scan.
_TOKEN_RE = re.compile(rf"^{re.escape(TOKEN_PREFIX)}([A-Za-z0-9_\-]{{16,128}})_([0-9a-f]{{{LOOKUP_LENGTH}}})$")


@dataclass(frozen=True)
class MintedKey:
    """The three values a freshly minted key produces.

    ``plaintext`` exists only here and in the single response that shows it to
    the operator. Nothing persists it, and nothing can recover it afterwards.
    """

    plaintext: str
    lookup_prefix: str
    token_digest: str


def lookup_for(secret: str) -> str:
    """The indexed, non-secret handle derived from a token's secret part."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:LOOKUP_LENGTH]


def digest_for(secret: str) -> str:
    """The stored, queryable fingerprint of a token's secret part."""
    return hmac_digest(secret)


def mint() -> MintedKey:
    """Generate a new API key."""
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    lookup = lookup_for(secret)
    return MintedKey(
        plaintext=f"{TOKEN_PREFIX}{secret}_{lookup}",
        lookup_prefix=lookup,
        token_digest=digest_for(secret),
    )


def parse(raw: str) -> tuple[str, str] | None:
    """Split a presented token into ``(secret, lookup_prefix)``.

    Total: every malformation — wrong prefix, wrong shape, wrong length,
    non-hex lookup, a lookup that does not match the secret — returns ``None``
    rather than raising, so the auth path has exactly one failure branch and
    therefore exactly one response.

    The lookup is recomputed and compared rather than trusted, so a caller
    cannot steer the index lookup at one key while presenting another's secret.
    """
    if not raw or not isinstance(raw, str) or len(raw) > 200:
        return None
    match = _TOKEN_RE.match(raw)
    if match is None:
        return None
    secret, lookup = match.group(1), match.group(2)
    if not secrets.compare_digest(lookup_for(secret), lookup):
        return None
    return secret, lookup
