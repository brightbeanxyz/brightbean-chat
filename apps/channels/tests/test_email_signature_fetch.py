"""The SNS certificate fetch really goes through the guard (SECURITY-BASELINE §6).

This module exists because of a gap issue #29's audit found rather than because
of a feature. ``email_signatures._public_key`` is one of four production calls
to ``guarded_request``, and it was the only one with no proof: its tests in
``test_email_webhooks.py`` install a ``sns_keypair`` fixture that **replaces**
``guarded_request`` with a stub. ``tests/ssrf.py`` opens by warning about
exactly that shape:

    Asserting that a patched ``guarded_request`` was called is not the same
    claim: it stays green when a second, unguarded request is made beside it.

So the fetch is exercised here with the real guard installed and
``guard_required()`` watching. A separate module rather than an addition to the
existing one, because that fixture is module-scoped and this needs the opposite
of it.

The URL is worth a sentence on its own. It comes out of an attacker-controlled
SNS payload; ``CERT_URL_RE`` pins it to an ``sns.<region>.amazonaws.com`` host,
and the guard is what stops a hostname AWS controls today from resolving
somewhere else tomorrow — the check-then-connect window the allowlist alone
cannot close.
"""

from typing import Any

import pytest

from apps.channels.providers import email_signatures
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

CERT_URL = "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc123.pem"
CERT_HOST = "sns.eu-west-1.amazonaws.com"

#: Not a real certificate — the fetch is what is under test, and
#: ``_public_key`` returns ``None`` on anything unparseable, which is the
#: refusal these tests want anyway.
NOT_A_CERT = b"-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    """``_public_key`` memoises by URL, so a warm cache would skip the fetch —
    and a test that never made a request proves nothing about how it is made."""
    email_signatures.clear_certificate_cache()
    yield
    email_signatures.clear_certificate_cache()


class TestTheCertificateFetchIsGuarded:
    def test_it_goes_through_the_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with deployment_cache_cleared():
            FakeInternet(serving(NOT_A_CERT), {CERT_HOST: [FakeInternet.PUBLIC]}).install(monkeypatch)

            with guard_required() as requests:
                email_signatures._public_key(CERT_URL)

        # One request, and it went out **pinned**: the URL carries the resolved
        # literal while the Host header keeps the name. That is the guard's
        # signature — it is what closes the window between the address it
        # validated and the address the socket would otherwise resolve again.
        assert len(requests) == 1
        assert requests[0].url.host == FakeInternet.PUBLIC
        assert requests[0].headers["Host"] == CERT_HOST
        assert requests[0].url.path == "/SimpleNotificationService-abc123.pem"

    def test_a_certificate_url_resolving_to_loopback_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The attack the allowlist cannot stop on its own: the host matches
        ``CERT_URL_RE`` and resolves somewhere private."""
        with deployment_cache_cleared():
            FakeInternet(serving(NOT_A_CERT), {CERT_HOST: ["127.0.0.1"]}).install(monkeypatch)

            with guard_required():
                key = email_signatures._public_key(CERT_URL)

        assert key is None

    def test_a_link_local_address_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """169.254.169.254 is the cloud metadata service, and an SES deployment
        is precisely the kind that has one."""
        with deployment_cache_cleared():
            FakeInternet(serving(NOT_A_CERT), {CERT_HOST: ["169.254.169.254"]}).install(monkeypatch)

            with guard_required():
                key = email_signatures._public_key(CERT_URL)

        assert key is None

    def test_a_refused_fetch_does_not_raise_into_the_webhook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A blocked URL is a refused delivery, not a 500 — the webhook path
        turns ``None`` into the same 403 a bad signature gets."""
        with deployment_cache_cleared():
            FakeInternet(serving(NOT_A_CERT), {CERT_HOST: ["10.0.0.5"]}).install(monkeypatch)

            with guard_required():
                assert email_signatures._public_key(CERT_URL) is None
