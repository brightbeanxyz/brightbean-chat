"""The webhook hardening helpers (SECURITY-BASELINE §§2, 4, 7)."""

from typing import Any

import pytest
from django.test import RequestFactory
from django.test.utils import override_settings

from apps.channels import security


class TestBodySizeCap:
    def test_a_body_within_the_cap_passes(self) -> None:
        request = RequestFactory().post("/webhooks/telegram/", data=b"{}", content_type="application/json")
        assert security.body_too_large(request) is False

    @override_settings(WEBHOOK_MAX_BODY_BYTES=10)
    def test_an_oversized_body_is_refused_from_the_header_alone(self) -> None:
        request = RequestFactory().post(
            "/webhooks/telegram/", data=b'{"x":"' + b"y" * 100 + b'"}', content_type="application/json"
        )
        assert security.body_too_large(request) is True

    def test_a_body_with_no_declared_length_is_refused(self) -> None:
        """Accepting it would mean reading an unbounded body to measure it."""
        request = RequestFactory().post("/webhooks/telegram/")
        request.META.pop("CONTENT_LENGTH", None)
        assert security.body_too_large(request) is True

    @pytest.mark.parametrize("value", ["", "abc", "-1x", "9999999999999999999999x"])
    def test_a_malformed_content_length_is_refused(self, value: str) -> None:
        request = RequestFactory().post("/webhooks/telegram/")
        request.META["CONTENT_LENGTH"] = value
        assert security.body_too_large(request) is True


class TestSignatures:
    def test_a_correct_signature_verifies(self) -> None:
        body = b'{"hello":"world"}'
        header = f"sha256={security.sign_body('s3cret', body)}"
        assert security.verify_signature_header(secret="s3cret", raw_body=body, header_value=header) is True

    def test_a_single_changed_byte_fails(self) -> None:
        body = b'{"hello":"world"}'
        header = f"sha256={security.sign_body('s3cret', body)}"
        assert security.verify_signature_header(secret="s3cret", raw_body=body + b" ", header_value=header) is False

    @pytest.mark.parametrize(
        "header",
        [
            None,
            "",
            "deadbeef",  # no prefix
            "sha256=",  # empty digest
            "sha1=deadbeef",  # wrong algorithm
            "sha256=not-hex-at-all",
            "sha256=" + "0" * 64,
        ],
    )
    def test_every_malformed_header_fails_the_same_way(self, header: str | None) -> None:
        """A distinguishable 'malformed header' reply would be a free oracle."""
        assert security.verify_signature_header(secret="s3cret", raw_body=b"{}", header_value=header) is False

    def test_no_secret_fails_closed(self) -> None:
        body = b"{}"
        header = f"sha256={security.sign_body('', body)}"
        assert security.verify_signature_header(secret="", raw_body=body, header_value=header) is False

    def test_signing_is_over_bytes_not_text(self) -> None:
        # Re-serialising parsed JSON changes whitespace and key order, so the
        # digest is computed on exactly what arrived.
        assert security.sign_body("k", b'{"a":1}') != security.sign_body("k", b'{"a": 1}')


class TestJsonShape:
    def test_depth_counts_nesting(self) -> None:
        assert security.max_json_depth(b"{}") == 1
        assert security.max_json_depth(b'{"a":[{"b":1}]}') == 3
        assert security.max_json_depth(b"[" * 40 + b"]" * 40) == 40

    def test_brackets_inside_strings_do_not_count(self) -> None:
        assert security.max_json_depth(b'{"a":"[[[[[[[[["}') == 1

    def test_escaped_quotes_inside_strings_do_not_confuse_the_scan(self) -> None:
        assert security.max_json_depth(rb'{"a":"he said \"[[[\" ok"}') == 1

    @override_settings(WEBHOOK_MAX_JSON_DEPTH=5)
    def test_a_nesting_bomb_is_rejected_without_parsing(self) -> None:
        bomb = b"[" * 2000 + b"]" * 2000
        assert security.parse_json_body(bomb) is None

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"not json",
            b"[1,2,3]",  # a list is not a payload we understand
            b'"just a string"',
            b"123",
            b"\xff\xfe not utf-8",
        ],
    )
    def test_anything_that_is_not_a_json_object_is_rejected(self, raw: bytes) -> None:
        assert security.parse_json_body(raw) is None

    def test_a_json_object_parses(self) -> None:
        assert security.parse_json_body(b'{"a": 1}') == {"a": 1}

    @override_settings(WEBHOOK_MAX_BODY_BYTES=10)
    def test_the_size_cap_applies_to_the_parse_too(self) -> None:
        assert security.parse_json_body(b'{"a":"' + b"y" * 100 + b'"}') is None


@pytest.mark.django_db
class TestSignatureFailureThrottle:
    @staticmethod
    def _request(ip: str = "203.0.113.5") -> Any:
        return RequestFactory().post("/webhooks/telegram/", REMOTE_ADDR=ip)

    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=3)
    def test_a_source_is_banned_after_the_limit(self) -> None:
        request = self._request()
        assert security.is_banned(request) is False
        for _ in range(3):
            assert security.record_signature_failure(request) is False
        assert security.record_signature_failure(request) is True
        assert security.is_banned(request) is True

    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=2)
    def test_the_ban_is_per_source(self) -> None:
        attacker = self._request("203.0.113.5")
        bystander = self._request("198.51.100.9")
        for _ in range(5):
            security.record_signature_failure(attacker)
        assert security.is_banned(attacker) is True
        assert security.is_banned(bystander) is False

    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=2)
    def test_a_connection_is_banned_independently_of_the_source(self) -> None:
        """Distributed guessing at one connection would slip past per-IP counting."""
        connection_id = "11111111-1111-1111-1111-111111111111"
        for index in range(5):
            security.record_signature_failure(self._request(f"203.0.113.{index}"), connection_id)
        fresh_source = self._request("198.51.100.9")
        assert security.is_banned(fresh_source) is False
        assert security.is_banned(fresh_source, connection_id) is True

    @override_settings(WEBHOOK_SIGNATURE_BAN_SECONDS=-1, WEBHOOK_SIGNATURE_FAILURE_LIMIT=1)
    def test_the_ban_lifts_when_it_expires(self) -> None:
        # A negative duration writes an already-expired ban row, which is the
        # same state the clock reaches on its own.
        request = self._request()
        security.record_signature_failure(request)
        security.record_signature_failure(request)
        assert security.is_banned(request) is False

    def test_the_forwarded_header_is_ignored_from_an_untrusted_peer(self) -> None:
        """Otherwise a client mints a fresh bucket per request and the ban is off."""
        request = RequestFactory().post(
            "/webhooks/telegram/",
            REMOTE_ADDR="203.0.113.5",
            HTTP_X_FORWARDED_FOR="1.2.3.4",
        )
        assert security.client_identity(request) == "203.0.113.5"

    @override_settings(TRUSTED_PROXIES=["203.0.113.5"])
    def test_a_trusted_proxy_can_speak_for_its_client(self) -> None:
        request = RequestFactory().post(
            "/webhooks/telegram/",
            REMOTE_ADDR="203.0.113.5",
            HTTP_X_FORWARDED_FOR="1.2.3.4",
        )
        assert security.client_identity(request) == "1.2.3.4"
