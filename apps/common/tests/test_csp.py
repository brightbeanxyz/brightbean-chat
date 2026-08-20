"""Content Security Policy with per-request nonces (SECURITY-BASELINE §8)."""

import re

import pytest

NONCE_RE = re.compile(r"nonce-([A-Za-z0-9+/=]+)")


@pytest.mark.django_db
class TestContentSecurityPolicy:
    def test_header_is_sent(self, client):
        response = client.get("/")

        assert "Content-Security-Policy" in response.headers

    def test_template_nonce_matches_the_header_nonce(self, client):
        response = client.get("/")

        header = response.headers["Content-Security-Policy"]
        header_nonce = NONCE_RE.search(header)
        assert header_nonce, f"no nonce in script-src: {header}"

        assert f'nonce="{header_nonce.group(1)}"'.encode() in response.content

    def test_nonce_changes_per_request(self, client):
        first = NONCE_RE.search(client.get("/").headers["Content-Security-Policy"])
        second = NONCE_RE.search(client.get("/").headers["Content-Security-Policy"])

        assert first.group(1) != second.group(1)

    def test_policy_directives(self, client):
        header = client.get("/").headers["Content-Security-Policy"]

        assert "default-src 'self'" in header
        assert "frame-ancestors 'none'" in header
        assert "object-src 'none'" in header
        assert "form-action 'self'" in header

    def test_unsafe_eval_is_scoped_to_scripts_and_unsafe_inline_to_styles(self, client):
        """Alpine's standard build needs eval; inline styles are Tailwind's."""
        header = client.get("/").headers["Content-Security-Policy"]
        directives = {part.split(" ", 1)[0]: part.split(" ", 1)[1] for part in header.split("; ") if " " in part}

        assert "'unsafe-eval'" in directives["script-src"]
        assert "'unsafe-inline'" not in directives["script-src"]
        assert "'unsafe-inline'" in directives["style-src"]
        assert "'unsafe-eval'" not in directives["style-src"]
