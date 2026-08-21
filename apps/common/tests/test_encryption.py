"""AES-256-GCM encrypted field round-trips (SECURITY-BASELINE §5)."""

import base64

import pytest
from cryptography.exceptions import InvalidTag
from django.db import connection

from apps.common.encryption import decrypt_value, encrypt_value
from tests.testapp.models import EncryptionProbe


class TestValueHelpers:
    def test_round_trip(self, secret_value):
        assert decrypt_value(encrypt_value(secret_value)) == secret_value

    def test_round_trip_unicode(self):
        plaintext = "Grüße 👋 — 日本語"
        assert decrypt_value(encrypt_value(plaintext)) == plaintext

    def test_each_encryption_uses_a_fresh_nonce(self, secret_value):
        first = encrypt_value(secret_value)
        second = encrypt_value(secret_value)

        assert first != second
        assert base64.b64decode(first)[:12] != base64.b64decode(second)[:12]
        assert decrypt_value(first) == decrypt_value(second) == secret_value

    def test_ciphertext_does_not_contain_the_plaintext(self, secret_value):
        assert secret_value not in encrypt_value(secret_value)

    def test_tampered_ciphertext_is_rejected(self, secret_value):
        raw = bytearray(base64.b64decode(encrypt_value(secret_value)))
        raw[-1] ^= 0xFF  # flip a bit in the GCM tag
        tampered = base64.b64encode(bytes(raw)).decode("ascii")

        # InvalidTag specifically: a bare Exception would also pass if some
        # unrelated line threw, which would hide AES-GCM authentication
        # silently breaking.
        with pytest.raises(InvalidTag):
            decrypt_value(tampered)

    def test_missing_salt_is_refused(self, settings, secret_value):
        settings.ENCRYPTION_KEY_SALT = b""

        with pytest.raises(ValueError, match="ENCRYPTION_KEY_SALT"):
            encrypt_value(secret_value)

    def test_a_different_salt_cannot_decrypt(self, settings, secret_value):
        ciphertext = encrypt_value(secret_value)
        settings.ENCRYPTION_KEY_SALT = b"a-completely-different-salt"

        with pytest.raises(InvalidTag):
            decrypt_value(ciphertext)


@pytest.mark.django_db
class TestEncryptedFields:
    def test_text_field_round_trips_through_the_database(self, secret_value):
        probe = EncryptionProbe.objects.create(secret=secret_value)

        assert EncryptionProbe.objects.get(pk=probe.pk).secret == secret_value

    def test_json_field_round_trips_through_the_database(self):
        payload = {"access_token": "abc123", "scopes": ["read", "write"], "expires_in": 3600}
        probe = EncryptionProbe.objects.create(payload=payload)

        assert EncryptionProbe.objects.get(pk=probe.pk).payload == payload

    def test_null_values_stay_null(self):
        probe = EncryptionProbe.objects.create(secret=None, payload=None)
        stored = EncryptionProbe.objects.get(pk=probe.pk)

        assert stored.secret is None
        assert stored.payload is None

    def test_stored_column_holds_ciphertext_not_plaintext(self, secret_value):
        probe = EncryptionProbe.objects.create(secret=secret_value, payload={"api_key": secret_value})

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT secret, payload FROM testapp_encryptionprobe WHERE id = %s",
                [str(probe.pk)],
            )
            raw_secret, raw_payload = cursor.fetchone()

        assert secret_value not in raw_secret
        assert secret_value not in raw_payload
        assert decrypt_value(raw_secret) == secret_value

    def test_lookups_on_encrypted_fields_never_match(self, secret_value):
        """Documents a footgun every later issue will otherwise walk into.

        Each write uses a fresh nonce, so the same plaintext encrypts to
        different ciphertext each time and a lookup compares two unrelated
        strings. It does not raise — it silently returns nothing, which reads
        as "no such row". Look records up by a separate deterministic column
        (an HMAC of the value) instead.
        """
        EncryptionProbe.objects.create(secret=secret_value)

        assert EncryptionProbe.objects.filter(secret=secret_value).count() == 0
        assert not EncryptionProbe.objects.filter(secret__contains=secret_value).exists()

    def test_corrupted_column_raises_on_read(self, secret_value):
        probe = EncryptionProbe.objects.create(secret=secret_value)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE testapp_encryptionprobe SET secret = %s WHERE id = %s",
                ["not-valid-ciphertext", str(probe.pk)],
            )

        with pytest.raises(ValueError, match="Decryption failed"):
            _ = EncryptionProbe.objects.get(pk=probe.pk).secret
