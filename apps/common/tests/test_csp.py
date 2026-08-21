"""Content Security Policy with per-request nonces (SECURITY-BASELINE §8)."""

import re

import pytest

NONCE_RE = re.compile(r"nonce-([A-Za-z0-9+/=]+)")

# A page that actually renders a template. "/" is the authenticated router and
# answers 302 for anonymous clients, and django-csp only mints a nonce when a
# template asks for one.
PAGE = "/accounts/login/"


@pytest.mark.django_db
class TestContentSecurityPolicy:
    def test_header_is_sent(self, client):
        response = client.get(PAGE)

        assert "Content-Security-Policy" in response.headers

    def test_template_nonce_matches_the_header_nonce(self, client):
        response = client.get(PAGE)

        header = response.headers["Content-Security-Policy"]
        header_nonce = NONCE_RE.search(header)
        assert header_nonce, f"no nonce in script-src: {header}"

        assert f'nonce="{header_nonce.group(1)}"'.encode() in response.content

    def test_nonce_changes_per_request(self, client):
        first = NONCE_RE.search(client.get(PAGE).headers["Content-Security-Policy"])
        second = NONCE_RE.search(client.get(PAGE).headers["Content-Security-Policy"])

        assert first.group(1) != second.group(1)

    def test_policy_directives(self, client):
        header = client.get(PAGE).headers["Content-Security-Policy"]

        assert "default-src 'self'" in header
        assert "frame-ancestors 'none'" in header
        assert "object-src 'none'" in header
        assert "form-action 'self'" in header

    def test_unsafe_eval_is_scoped_to_scripts_and_unsafe_inline_to_styles(self, client):
        """Alpine's standard build needs eval; inline styles are Tailwind's."""
        header = client.get(PAGE).headers["Content-Security-Policy"]
        directives = {part.split(" ", 1)[0]: part.split(" ", 1)[1] for part in header.split("; ") if " " in part}

        assert "'unsafe-eval'" in directives["script-src"]
        assert "'unsafe-inline'" not in directives["script-src"]
        assert "'unsafe-inline'" in directives["style-src"]
        assert "'unsafe-eval'" not in directives["style-src"]


class TestStorageOrigin:
    """CSP sources derived from S3_CUSTOM_DOMAIN / S3_ENDPOINT_URL.

    Studio prepends "https://" to anything not already starting with it and
    then rebuilds the origin from ``.hostname``, so an http:// MinIO endpoint
    becomes "https://http://localhost:9000" and any non-default port is lost —
    either way the source never matches the media URLs the page loads and the
    browser blocks every image.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("http://localhost:9000", "http://localhost:9000"),
            ("https://minio.example.com:9000", "https://minio.example.com:9000"),
            ("https://cdn.example.com", "https://cdn.example.com"),
            # A bare domain is the usual shape of S3_CUSTOM_DOMAIN.
            ("cdn.example.com", "https://cdn.example.com"),
            # Userinfo is not part of a CSP source, and would be a credential
            # published in a response header.
            ("https://key:secret@cdn.example.com", "https://cdn.example.com"),
            # IPv6 literals keep their brackets or the port cannot be parsed.
            ("http://[::1]:9000", "http://[::1]:9000"),
            ("", None),
            ("   ", None),
        ],
    )
    def test_origin_keeps_scheme_and_port(self, value, expected):
        from config.settings.base import csp_origin

        assert csp_origin(value) == expected
