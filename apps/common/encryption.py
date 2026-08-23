"""AES-256-GCM encrypted model fields.

Ported near-verbatim from BrightBean Studio's ``apps/common/encryption.py``.
The key is derived from ``SECRET_KEY`` via HKDF-SHA256 salted with
``settings.ENCRYPTION_KEY_SALT``; values are stored as base64 of a 12-byte
nonce concatenated with the AES-GCM ciphertext (which carries its own tag).

The only deliberate change from Studio is the HKDF ``info`` constant, so a
key derived for Chat can never decrypt a Studio ciphertext or vice versa.

Every credential or token this project persists goes in one of these fields —
never a plain column (SECURITY-BASELINE §5).

Where a row has to be found *by* a secret rather than by its owner, use
:func:`hmac_digest` to store a deterministic sidecar column and query that. It
is keyed on ``SECRET_KEY``, so a stolen database gives up neither the values nor
the ability to recompute them without the key.

**These encrypted fields cannot be used in queryset lookups.** Every write encrypts under
a fresh random nonce, so the same plaintext produces different ciphertext every
time and ``.filter(secret=value)`` compares two unrelated strings. It does not
raise — it silently matches nothing, which reads as "no such row" and is
miserable to debug. To look a record up by a secret (resolving an inbound
webhook to its connection, say), store a separate deterministic column
alongside — an HMAC of the value under ``SECRET_KEY`` — and query that.
"""

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

# Domain separation for the derived key. Do not change: it would make every
# stored ciphertext undecryptable.
HKDF_INFO = b"brightbean-chat-field-encryption"

# Separate domain for the lookup digest, so a digest can never collide with, or
# be mistaken for, key material derived for encryption.
HKDF_DIGEST_INFO = b"brightbean-chat-lookup-digest"

NONCE_BYTES = 12


@lru_cache(maxsize=8)
def _hkdf(secret: bytes, salt: bytes) -> bytes:
    """HKDF-SHA256 over (secret, salt), memoised.

    Studio re-derives on every single field read and write, so listing a
    thousand rows with an encrypted column performs a thousand derivations of
    a key that is constant for the life of the process. Keying the cache on
    the inputs rather than memoising a no-argument function keeps
    ``override_settings`` and the ``settings`` fixture working: change either
    input and you get a different entry, not a stale key.
    """
    return HKDF(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
    ).derive(secret)


def _derive_key() -> bytes:
    """Derive a 256-bit encryption key from SECRET_KEY via HKDF-SHA256."""
    secret = settings.SECRET_KEY.encode("utf-8")
    salt = getattr(settings, "ENCRYPTION_KEY_SALT", None)
    if not salt:
        raise ValueError(
            "ENCRYPTION_KEY_SALT must be set. Generate a random value and add it "
            "to your environment variables. This is required for secure encryption."
        )
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return _hkdf(secret, salt)


def hmac_digest(value: str) -> str:
    """A deterministic, queryable fingerprint of a secret.

    Encrypted columns cannot be filtered (see above), so a table that has to be
    looked up *by* a credential — an invitation token arriving in a URL, a
    webhook secret arriving in a header — stores this alongside, or instead of,
    the value itself.

    HMAC-SHA256 keyed on a value derived from ``SECRET_KEY``, not a bare hash:
    an unkeyed digest of a token is offline-guessable at whatever rate the
    token's entropy allows, and gives an attacker holding a database dump a
    verification oracle. Keyed, the dump alone is useless.

    Deterministic by design, which is the whole point — the same input always
    produces the same digest, so it can carry a unique constraint and an index.
    """
    key = _hkdf(settings.SECRET_KEY.encode("utf-8"), _digest_salt())
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _digest_salt() -> bytes:
    """The HKDF salt for lookup digests. Same source as the encryption salt."""
    salt = getattr(settings, "ENCRYPTION_KEY_SALT", None)
    if not salt:
        raise ValueError(
            "ENCRYPTION_KEY_SALT must be set. Generate a random value and add it "
            "to your environment variables. This is required for secure encryption."
        )
    if isinstance(salt, str):
        salt = salt.encode("utf-8")
    return salt + HKDF_DIGEST_INFO


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string and return base64-encoded nonce+ciphertext."""
    key = _derive_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_value(encrypted: str) -> str:
    """Decrypt a base64-encoded nonce+ciphertext string."""
    key = _derive_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce = raw[:NONCE_BYTES]
    ciphertext = raw[NONCE_BYTES:]
    return aesgcm.decrypt(nonce, ciphertext, None).decode("utf-8")


class EncryptedTextField(models.TextField):
    """A TextField that encrypts its value at rest using AES-256-GCM.

    Not usable in queryset lookups — ``.filter()``/``.get()`` on this field
    silently match nothing. See the module docstring.
    """

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return encrypt_value(str(value))

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str | None:
        if value is None:
            return None
        try:
            return decrypt_value(value)
        except (InvalidTag, ValueError, binascii.Error) as e:
            # Log the exception type only — never the value, which is the
            # ciphertext of a credential.
            logger.error("Failed to decrypt EncryptedTextField: %s", type(e).__name__)
            raise ValueError("Decryption failed - possibly wrong SECRET_KEY or corrupted data") from e

    def to_python(self, value: Any) -> Any:
        return value


class EncryptedJSONField(models.TextField):
    """A field that stores JSON data encrypted at rest using AES-256-GCM.

    Not usable in queryset lookups — ``.filter()``/``.get()`` on this field
    silently match nothing. See the module docstring.
    """

    def get_prep_value(self, value: Any) -> str | None:
        if value is None:
            return None
        return encrypt_value(json.dumps(value))

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        if value is None:
            return None
        try:
            return json.loads(decrypt_value(value))
        except (InvalidTag, ValueError, binascii.Error) as e:
            logger.error("Failed to decrypt EncryptedJSONField: %s", type(e).__name__)
            raise ValueError("Decryption failed - possibly wrong SECRET_KEY or corrupted data") from e

    def to_python(self, value: Any) -> Any:
        if isinstance(value, dict | list):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                # Value is likely already-encrypted ciphertext from the DB,
                # which will be handled by from_db_value. Return as-is.
                return value
        return value
