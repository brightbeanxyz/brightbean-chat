"""Client-address resolution behind proxies (deviation 7)."""

from typing import Any

import pytest
from django.test import RequestFactory

from apps.common.net import get_client_ip, is_trusted_proxy

rf = RequestFactory()


def _request(remote_addr: str, forwarded: str | None = None) -> Any:
    request = rf.get("/")
    request.META["REMOTE_ADDR"] = remote_addr
    if forwarded is not None:
        request.META["HTTP_X_FORWARDED_FOR"] = forwarded
    return request


class TestUntrustedByDefault:
    def test_x_forwarded_for_is_ignored_when_nothing_is_trusted(self):
        """Studio takes the leftmost value unconditionally, which lets any
        client mint a fresh rate-limit bucket per request."""
        request = _request("203.0.113.9", forwarded="1.2.3.4")

        assert get_client_ip(request) == "203.0.113.9"

    def test_trusted_proxies_defaults_to_empty(self, settings):
        assert settings.TRUSTED_PROXIES == []


class TestBehindATrustedProxy:
    """``settings`` (pytest-django) rather than a class-level
    ``override_settings``, which Django only supports on SimpleTestCase."""

    @pytest.fixture(autouse=True)
    def _trusted(self, settings):
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]

    def test_the_client_is_taken_from_the_forwarded_chain(self):
        request = _request("10.1.2.3", forwarded="203.0.113.9")

        assert get_client_ip(request) == "203.0.113.9"

    def test_trusted_hops_are_peeled_from_the_right(self):
        """The chain appends on the right; the leftmost entry is client-written."""
        request = _request("10.1.2.3", forwarded="203.0.113.9, 10.4.4.4, 10.5.5.5")

        assert get_client_ip(request) == "203.0.113.9"

    def test_a_forged_prefix_cannot_impersonate(self):
        """A client sending its own X-Forwarded-For only prepends to the chain."""
        request = _request("10.1.2.3", forwarded="1.2.3.4, 203.0.113.9")

        assert get_client_ip(request) == "203.0.113.9"

    def test_an_untrusted_peer_is_still_ignored(self):
        request = _request("198.51.100.7", forwarded="1.2.3.4")

        assert get_client_ip(request) == "198.51.100.7"

    def test_an_empty_chain_falls_back_to_the_peer(self):
        request = _request("10.1.2.3", forwarded="")

        assert get_client_ip(request) == "10.1.2.3"

    def test_an_all_trusted_chain_falls_back_to_the_peer(self):
        request = _request("10.1.2.3", forwarded="10.9.9.9")

        assert get_client_ip(request) == "10.1.2.3"


class TestTrustedProxyParsing:
    def test_a_bare_address_works(self, settings):
        settings.TRUSTED_PROXIES = ["127.0.0.1"]

        assert is_trusted_proxy("127.0.0.1")
        assert not is_trusted_proxy("127.0.0.2")

    def test_ipv6_works(self, settings):
        settings.TRUSTED_PROXIES = ["::1"]

        assert is_trusted_proxy("::1")

    def test_an_unparseable_entry_is_dropped_rather_than_widening_trust(self, settings):
        settings.TRUSTED_PROXIES = ["not-an-address", "10.0.0.0/8"]

        assert not is_trusted_proxy("not-an-address")
        assert is_trusted_proxy("10.1.1.1")

    def test_a_malformed_peer_address_is_not_trusted(self, settings):
        settings.TRUSTED_PROXIES = ["10.0.0.0/8"]

        assert not is_trusted_proxy("garbage")


class TestSettingsDoNotContradictDevelopment:
    def test_trusted_proxies_is_independent_of_the_proxy_ssl_header(self):
        """development.py trusts X-Forwarded-Proto/Host for tunnelled webhook
        work. That governs the request's scheme and host; TRUSTED_PROXIES
        governs client identity for rate limiting, and stays closed."""
        from config.settings import development

        assert development.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
        assert development.USE_X_FORWARDED_HOST is True
        assert getattr(development, "TRUSTED_PROXIES", []) == []
