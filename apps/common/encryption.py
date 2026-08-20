"""AES-256-GCM encrypted model fields.

Ported near-verbatim from BrightBean Studio's ``apps/common/encryption.py``.
The key is derived from ``SECRET_KEY`` via HKDF-SHA256 salted with
``settings.ENCRYPTION_KEY_SALT``; values are stored as base64 of a 12-byte
nonce concatenated with the AES-GCM ciphertext (which carries its own tag).

The only deliberate change from Studio is the HKDF ``info`` constant, so a
key derived for Chat can never decrypt a Studio ciphertext or vice versa.

Every credential or token this project persists goes in one of these fields —
never a plain column (SECURITY-BASELINE §5).
"""

import base64
import binascii
import json
import logging
import os
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

NONCE_BYTES = 12


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
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        info=HKDF_INFO,
    )
    return hkdf.derive(secret)


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
    """A TextField that encrypts its value at rest using AES-256-GCM."""

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
    """A field that stores JSON data encrypted at rest using AES-256-GCM."""

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
