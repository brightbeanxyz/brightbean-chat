"""Key material: how it is minted, stored, compared and revoked.

The claim under test is the one CONTRIBUTING.md makes about anything a caller
presents to prove they may act — *"kept as ``hmac_digest(value)`` in a queryable
column, and the raw value lives only in the message that delivered it"*. So the
assertions here are deliberately about the database and the wire, not about the
Python objects: a test that only checked ``api_key.token_digest != plaintext``
would pass on an implementation that also stashed the plaintext in a second
column.
"""

from __future__ import annotations

import logging

import pytest
from django.db import connection

from apps.api import keys as key_tokens
from apps.api.models import ApiKey, touch_last_used
from apps.api.tests.conftest import make_key
from apps.common.logging import REDACTED, scrub


class TestTokenFormat:
    def test_mint_produces_a_prefixed_token_that_parses(self):
        minted = key_tokens.mint()

        assert minted.plaintext.startswith(key_tokens.TOKEN_PREFIX)
        parsed = key_tokens.parse(minted.plaintext)
        assert parsed is not None
        secret, lookup = parsed
        assert lookup == minted.lookup_prefix
        assert key_tokens.digest_for(secret) == minted.token_digest

    def test_every_key_is_distinct(self):
        assert len({key_tokens.mint().plaintext for _ in range(20)}) == 20

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not-a-key",
            "bb_short_0011aabb",
            "oc_" + "a" * 43 + "_0011aabb",  # the pre-rebrand prefix is not accepted
            "bb_" + "a" * 43,  # no lookup segment
            "bb_" + "a" * 43 + "_ZZZZZZZZ",  # lookup is not hex
            "bb_" + "a" * 43 + "_0011aabbcc",  # lookup is the wrong length
            "bb_" + "a" * 500 + "_0011aabb",  # absurdly long
        ],
    )
    def test_malformed_tokens_parse_to_none_rather_than_raising(self, raw):
        assert key_tokens.parse(raw) is None

    def test_a_tampered_lookup_segment_is_refused(self):
        """The lookup is recomputed, not trusted.

        Otherwise a caller could steer the indexed lookup at one key's row while
        presenting a different secret — which is only stopped by the digest
        compare, and defence in depth is cheaper than relying on one check.
        """
        minted = key_tokens.mint()
        secret = minted.plaintext[len(key_tokens.TOKEN_PREFIX) :].rsplit("_", 1)[0]
        forged = f"{key_tokens.TOKEN_PREFIX}{secret}_deadbeef"

        assert key_tokens.parse(forged) is None


@pytest.mark.django_db
class TestStorage:
    def test_the_plaintext_is_nowhere_in_the_table(self, tenancy):
        api_key, plaintext = make_key(tenancy.workspace)
        secret = plaintext[len(key_tokens.TOKEN_PREFIX) :].rsplit("_", 1)[0]

        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM api_api_key WHERE id = %s", [str(api_key.pk)])
            columns = [column.name for column in cursor.description]
            row = cursor.fetchone()

        stored = " ".join(str(value) for value in row)
        assert plaintext not in stored
        assert secret not in stored
        # And the digest genuinely is there, so the assertion above is not
        # passing because the row is empty.
        assert api_key.token_digest in stored
        assert "token_digest" in columns

    def test_matches_accepts_the_right_secret_and_nothing_else(self, tenancy):
        api_key, plaintext = make_key(tenancy.workspace)
        secret = plaintext[len(key_tokens.TOKEN_PREFIX) :].rsplit("_", 1)[0]

        assert api_key.matches(secret)
        assert not api_key.matches(secret + "x")
        assert not api_key.matches("")

    def test_the_lookup_prefix_is_not_unique_so_a_collision_cannot_break_issuance(self, tenancy):
        """Two rows may share a prefix; the digest is what is unique.

        Eight hex characters is 32 bits, so a birthday collision arrives around
        65k keys. A unique index there would turn that into a random,
        unexplainable issuance failure.
        """
        first, _ = make_key(tenancy.workspace)
        second, _ = make_key(tenancy.workspace, name="second")
        ApiKey.objects.for_workspace(tenancy.workspace).filter(pk=second.pk).update(lookup_prefix=first.lookup_prefix)

        assert ApiKey.objects.for_workspace(tenancy.workspace).filter(lookup_prefix=first.lookup_prefix).count() == 2

    def test_touch_last_used_is_debounced(self, tenancy):
        """One write a minute, not one a request.

        Without the debounce a 10 req/s key writes 10 rows a second of pure
        bookkeeping on the hot path.
        """
        api_key, _ = make_key(tenancy.workspace)

        touch_last_used(api_key)
        first = ApiKey.objects.for_workspace(tenancy.workspace).get(pk=api_key.pk).last_used_at
        assert first is not None

        touch_last_used(api_key)
        assert ApiKey.objects.for_workspace(tenancy.workspace).get(pk=api_key.pk).last_used_at == first


class TestScrubbing:
    def test_a_key_in_a_log_line_is_redacted(self):
        """SECURITY-BASELINE §5: tokens never appear in captured logs.

        The shape is registered in ``apps.common.logging`` beside the Stripe,
        GitHub and Telegram shapes, so it is redacted even where there is no
        ``key=`` for the generic rule to anchor on — which is the form an
        ``Authorization`` header takes when it lands in an exception.
        """
        plaintext = key_tokens.mint().plaintext

        assert plaintext not in scrub(f"Authorization: Bearer {plaintext}")
        assert plaintext not in scrub(f"call failed for {plaintext} on /api/v1/contacts")
        assert REDACTED in scrub(plaintext)

    def test_the_scrubber_reaches_records_the_filter_never_sees(self, caplog):
        plaintext = key_tokens.mint().plaintext
        with caplog.at_level(logging.INFO):
            logging.getLogger("apps.api.tests").info("key=%s", plaintext)

        assert plaintext not in caplog.text
