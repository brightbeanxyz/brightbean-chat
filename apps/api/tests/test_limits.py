"""Input limits and the cursor — SECURITY-BASELINE §7.

Both caps are applied in the auth callback, which is the last hook before Ninja
hands the body to Pydantic. That placement is the point: §7 wants the cap
"before signature work where possible and always before DB writes", and a depth
check has to see the *bytes* — Python's JSON parser recurses, so a nesting bomb
is a stack overflow rather than a catchable exception.
"""

import json

import pytest

from apps.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, clamp_limit, decode_cursor, encode_cursor
from apps.api.tests.conftest import bearer

CONTACTS = "/api/v1/contacts"


@pytest.mark.django_db
class TestBodyLimits:
    def test_an_over_sized_body_is_refused_before_it_is_parsed(self, client, tenancy, api_key, settings):
        settings.API_MAX_BODY_BYTES = 512
        payload = json.dumps({"first_name": "x" * 2000})

        response = client.post(CONTACTS, data=payload, content_type="application/json", **bearer(api_key[1]))

        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

    def test_a_nesting_bomb_is_refused(self, client, tenancy, api_key, settings):
        settings.API_MAX_JSON_DEPTH = 5
        nested = "[" * 40 + "]" * 40

        response = client.post(CONTACTS, data=nested, content_type="application/json", **bearer(api_key[1]))

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "body_too_deep"

    def test_a_normal_body_is_unaffected(self, client, tenancy, api_key):
        response = client.post(
            CONTACTS,
            data=json.dumps({"first_name": "Ada"}),
            content_type="application/json",
            **bearer(api_key[1]),
        )

        assert response.status_code == 201

    def test_a_get_does_not_pay_for_the_body_read(self, client, tenancy, api_key, settings):
        """Methods with no body skip the read entirely."""
        settings.API_MAX_JSON_DEPTH = 1

        assert client.get(CONTACTS, **bearer(api_key[1])).status_code == 200


class TestCursor:
    def test_it_round_trips(self):
        assert decode_cursor(encode_cursor(0)) == 0
        assert decode_cursor(encode_cursor(137)) == 137

    def test_no_cursor_is_the_start(self):
        assert decode_cursor(None) == 0
        assert decode_cursor("") == 0

    @pytest.mark.parametrize(
        "cursor",
        [
            "not-base64!!",
            encode_cursor(0)[:-2] + "??",
            "eyJvIjogLTF9",  # {"o": -1}
            "eyJvIjogdHJ1ZX0",  # {"o": true} — bool is an int subclass
            "eyJ4IjogMX0" + "=" * 0,  # {"x": 1}: no offset key is fine, but…
            "W10",  # [] — not an object
            "x" * 300,
        ],
    )
    def test_a_malformed_cursor_raises_rather_than_returning_a_guess(self, cursor):
        if cursor == "eyJ4IjogMX0":
            # A well-formed object without "o" defaults to the start; that is
            # not a malformation, and it is here so the list above is honest.
            assert decode_cursor(cursor) == 0
            return
        with pytest.raises(ValueError):
            decode_cursor(cursor)

    def test_the_limit_is_clamped_at_both_ends(self):
        assert clamp_limit(None) == DEFAULT_LIMIT
        assert clamp_limit(0) == DEFAULT_LIMIT
        assert clamp_limit(-5) == 1
        assert clamp_limit(10_000) == MAX_LIMIT
        assert clamp_limit(7) == 7
