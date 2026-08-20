"""The shared signing utility (SECURITY-BASELINE §4)."""

from datetime import timedelta

import pytest
from django.http import Http404

from apps.common.signing import CURRENT_VERSION, InvalidTokenError, sign, unsign, unsign_or_404


class TestRoundTrip:
    def test_payload_survives_signing(self):
        token = sign({"contact": "abc", "campaign": 12}, purpose="unsubscribe")

        assert unsign(token, purpose="unsubscribe") == {"contact": "abc", "campaign": 12}

    def test_version_key_is_stripped_from_the_payload(self):
        payload = unsign(sign({"a": 1}, purpose="tick"), purpose="tick")

        assert "v" not in payload

    def test_version_key_is_reserved(self):
        with pytest.raises(ValueError, match="reserved"):
            sign({"v": 9}, purpose="tick")

    def test_token_is_url_safe(self):
        token = sign({"contact": "abc"}, purpose="click")

        assert set(token) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:.")


class TestRejection:
    def test_wrong_purpose_is_rejected(self):
        """A token minted for one route must not work on another."""
        token = sign({"contact": "abc"}, purpose="unsubscribe")

        with pytest.raises(InvalidTokenError):
            unsign(token, purpose="tick")

    def test_tampered_token_is_rejected(self):
        token = sign({"contact": "abc"}, purpose="click")
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

        with pytest.raises(InvalidTokenError):
            unsign(tampered, purpose="click")

    def test_expired_token_is_rejected(self):
        token = sign({"contact": "abc"}, purpose="preview")

        with pytest.raises(InvalidTokenError):
            unsign(token, purpose="preview", max_age=timedelta(seconds=-1))

    def test_unexpired_token_is_accepted(self):
        token = sign({"contact": "abc"}, purpose="preview")

        assert unsign(token, purpose="preview", max_age=3600) == {"contact": "abc"}

    def test_unknown_version_is_rejected(self):
        token = sign({"contact": "abc"}, purpose="click", version=CURRENT_VERSION + 1)

        with pytest.raises(InvalidTokenError):
            unsign(token, purpose="click")

    @pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "x" * 500])
    def test_malformed_tokens_are_rejected(self, token):
        with pytest.raises(InvalidTokenError):
            unsign(token, purpose="click")


class TestVersionMigration:
    """A format change has to be a rollout, not a cutover.

    Unsubscribe links are minted with ``max_age=None`` and live in recipients'
    inboxes indefinitely, so the day CURRENT_VERSION moves, the previous
    version must keep verifying or every one of those links 404s.
    """

    def test_a_reader_can_accept_more_than_one_version(self):
        old = sign({"contact": "abc"}, purpose="unsubscribe", version=1)
        new = sign({"contact": "abc"}, purpose="unsubscribe", version=2)

        assert unsign(old, purpose="unsubscribe", accept_versions=(1, 2)) == {"contact": "abc"}
        assert unsign(new, purpose="unsubscribe", accept_versions=(1, 2)) == {"contact": "abc"}

    def test_dropping_a_version_from_the_accept_list_rejects_it(self):
        old = sign({"contact": "abc"}, purpose="unsubscribe", version=1)

        with pytest.raises(InvalidTokenError):
            unsign(old, purpose="unsubscribe", accept_versions=(2,))

    def test_the_default_accepts_only_the_current_version(self):
        old = sign({"contact": "abc"}, purpose="unsubscribe", version=CURRENT_VERSION + 1)

        with pytest.raises(InvalidTokenError):
            unsign(old, purpose="unsubscribe")

    def test_or_404_honours_the_accept_list(self):
        old = sign({"contact": "abc"}, purpose="unsubscribe", version=1)

        assert unsign_or_404(old, purpose="unsubscribe", accept_versions=(1, 2)) == {"contact": "abc"}


class TestGeneric404:
    def test_valid_token_passes_through(self):
        token = sign({"contact": "abc"}, purpose="click")

        assert unsign_or_404(token, purpose="click") == {"contact": "abc"}

    @pytest.mark.parametrize("token", ["garbage", "", "x" * 200])
    def test_every_failure_is_an_undetailed_404(self, token):
        with pytest.raises(Http404) as exc_info:
            unsign_or_404(token, purpose="click")

        # No detail at all: a caller must not be able to tell a bad signature
        # from a wrong purpose from an expired token.
        assert str(exc_info.value) == ""

    def test_wrong_purpose_is_also_a_404(self):
        token = sign({"contact": "abc"}, purpose="unsubscribe")

        with pytest.raises(Http404):
            unsign_or_404(token, purpose="tick")
