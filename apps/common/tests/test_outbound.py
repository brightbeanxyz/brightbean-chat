"""The SSRF guard — SECURITY-BASELINE §6, issue #15's acceptance table.

Nothing here opens a socket. DNS is patched at :func:`resolve_host`, the one
place the guard resolves anything, and the transport is ``httpx.MockTransport``
— which is also what lets the pinning assertions work: the handler sees the
request the guard actually built, so "did it connect to the literal address and
claim the original ``Host``?" is a fact the test reads rather than infers.
"""

import ipaddress
from typing import Any

import httpx
import pytest

from apps.common import outbound
from apps.common.outbound import (
    GUARD_EXTENSION,
    MAX_REDIRECTS,
    BlockedURLError,
    GuardedResponse,
    OutboundTransportError,
    guarded_request,
)

PUBLIC = "93.184.216.34"
PUBLIC_ALT = "93.184.216.35"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"

#: Issue #15's table, plus the address that matters most in practice:
#: 169.254.169.254 is the cloud instance-metadata service, and reaching it is
#: how an SSRF becomes stolen cloud credentials.
PRIVATE_ADDRESSES = [
    "127.0.0.1",
    "127.16.3.4",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "::1",
    "fd00::1",
    "fe80::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:169.254.169.254",
    "2002:7f00:0001::",  # 6to4 wrapping 127.0.0.1
    "0.0.0.0",  # noqa: S104 - denying it is the assertion, not binding to it
    "224.0.0.1",
    # Carrier-grade NAT. Neither is_private nor is_reserved in Python's
    # ipaddress, so a guard written as "deny private" allows it — see _refusal.
    "100.64.0.1",
]


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch: Any) -> None:
    """No test in this file touches the network, including for DNS.

    Every unstubbed name answers "does not resolve", which is what a real
    resolver would say about ``example.test``. Tests that need a name to point
    somewhere call :func:`resolving`, which replaces ``resolve_host`` outright —
    address literals keep working either way, because ``resolve_host``
    short-circuits them before ``getaddrinfo`` is reached.
    """

    def _gaierror(*args: Any, **kwargs: Any) -> Any:
        raise outbound.socket.gaierror("no such host")

    monkeypatch.setattr(outbound.socket, "getaddrinfo", _gaierror)


def resolving(mapping: dict[str, list[str]], monkeypatch: Any, *, calls: list[str] | None = None) -> None:
    """Point :func:`resolve_host` at a table. Unknown names do not resolve."""

    def _resolve(host: str) -> tuple[Any, ...]:
        if calls is not None:
            calls.append(host)
        return tuple(ipaddress.ip_address(value) for value in mapping.get(host.lower(), []))

    monkeypatch.setattr(outbound, "resolve_host", _resolve)


def transport(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(payload: Any = None, **kwargs: Any) -> Any:
    return lambda request: httpx.Response(200, json=payload if payload is not None else {"ok": True}, **kwargs)


def unreachable(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the guard connected to {request.url} when it should have refused")


class TestAddressRules:
    @pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
    def test_an_address_literal_is_refused(self, address: str) -> None:
        host = f"[{address}]" if ":" in address else address
        with pytest.raises(BlockedURLError):
            guarded_request("GET", f"http://{host}/", client=transport(unreachable))

    @pytest.mark.parametrize("address", PRIVATE_ADDRESSES)
    def test_a_name_resolving_to_one_is_refused(self, address: str, monkeypatch: Any) -> None:
        """The interesting half: the URL looks entirely ordinary."""
        resolving({"internal.example.test": [address]}, monkeypatch)
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "https://internal.example.test/", client=transport(unreachable))

    def test_one_bad_address_among_good_ones_refuses_the_whole_name(self, monkeypatch: Any) -> None:
        """A name answering both is rebinding with the timing taken out."""
        resolving({"split.example.test": [PUBLIC, "127.0.0.1"]}, monkeypatch)
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "https://split.example.test/", client=transport(unreachable))

    def test_a_name_that_does_not_resolve_is_refused(self, monkeypatch: Any) -> None:
        resolving({}, monkeypatch)
        with pytest.raises(BlockedURLError, match="does not resolve"):
            guarded_request("GET", "https://nowhere.example.test/", client=transport(unreachable))

    def test_a_public_address_is_allowed(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        response = guarded_request("GET", "https://api.example.test/v1", client=transport(ok()))
        assert response.status_code == 200
        assert response.json() == {"ok": True}


class TestSchemes:
    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://127.0.0.1:11211/_stats",
            "ftp://files.example.test/x",
            "data:text/plain,hello",
            "//example.test/x",
        ],
    )
    def test_only_http_and_https_are_requested(self, url: str) -> None:
        with pytest.raises(BlockedURLError):
            guarded_request("GET", url, client=transport(unreachable))

    def test_a_url_with_credentials_in_it_is_refused(self, monkeypatch: Any) -> None:
        """``https://api.example.test@evil.test/`` reads as one host and is another."""
        resolving({"evil.test": [PUBLIC]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="username or password"):
            guarded_request("GET", "https://api.example.test@evil.test/", client=transport(unreachable))

    def test_an_unparseable_url_is_a_refusal_not_a_crash(self) -> None:
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "http://[oops", client=transport(unreachable))


class TestTheDeploymentsOwnHost:
    @pytest.fixture(autouse=True)
    def _deployment(self, settings: Any) -> None:
        settings.APP_URL = "https://chat.example.test"
        settings.ALLOWED_HOSTS = ["chat.example.test", "www.example.test"]

    def test_the_app_url_host_is_refused(self, monkeypatch: Any) -> None:
        resolving({"chat.example.test": [PUBLIC]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", "https://chat.example.test/internal/tick", client=transport(unreachable))

    def test_it_is_refused_on_any_port(self, monkeypatch: Any) -> None:
        """SPEC §11.7 says "the deployment's own host"; behind a proxy it answers on several."""
        resolving({"chat.example.test": [PUBLIC]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", "http://chat.example.test:8000/admin/", client=transport(unreachable))

    def test_an_allowed_hosts_entry_is_refused(self, monkeypatch: Any) -> None:
        resolving({"www.example.test": [PUBLIC]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", "https://www.example.test/", client=transport(unreachable))

    def test_reaching_it_by_address_is_refused_too(self, monkeypatch: Any) -> None:
        """The name check alone would be defeated by typing the IP instead."""
        resolving({"chat.example.test": [PUBLIC], PUBLIC: [PUBLIC]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", f"https://{PUBLIC}/internal/tick", client=transport(unreachable))

    def test_a_wildcard_allowed_hosts_does_not_deny_everything(self, monkeypatch: Any, settings: Any) -> None:
        """Development sets ``["*"]``; reading it as a hostname would break every flow."""
        settings.ALLOWED_HOSTS = ["*"]
        resolving({"api.example.test": [PUBLIC], "chat.example.test": [PUBLIC_ALT]}, monkeypatch)
        assert guarded_request("GET", "https://api.example.test/", client=transport(ok())).ok

    def test_a_deployment_whose_own_name_does_not_resolve_still_works(self, monkeypatch: Any) -> None:
        """Ordinary inside a private network — and not a reason to refuse every URL.

        ``resolve_host`` returns nothing for ``chat.example.test`` here, so only
        the name check can fire — and the URL under test is a different name.
        """
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        assert guarded_request("GET", "https://api.example.test/", client=transport(ok())).ok


class TestPinning:
    def test_the_connection_uses_the_resolved_address(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request("GET", "https://api.example.test/v1/orders?x=1", client=transport(handler))

        request = seen[0]
        assert request.url.host == PUBLIC, "the socket must go to the checked address, not to a fresh lookup"
        assert request.url.path == "/v1/orders"
        assert request.url.params["x"] == "1"
        assert request.headers["host"] == "api.example.test"
        assert request.extensions["sni_hostname"] == "api.example.test"
        assert request.extensions[GUARD_EXTENSION] is True

    def test_an_ipv6_target_is_pinned_in_brackets(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC_V6]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request("GET", "https://api.example.test/v1", client=transport(handler))
        assert seen[0].url.host == PUBLIC_V6
        assert seen[0].headers["host"] == "api.example.test"

    def test_a_non_default_port_survives_pinning(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request("GET", "https://api.example.test:8443/v1", client=transport(handler))
        assert seen[0].url.port == 8443
        assert seen[0].headers["host"] == "api.example.test:8443"

    def test_dns_rebinding_cannot_move_the_connection(self, monkeypatch: Any) -> None:
        """The attack the pinning exists for.

        The nameserver answers with a public address while the checks run and a
        loopback address a millisecond later. A guard that let httpx resolve the
        name would connect to 127.0.0.1 with every check having passed.
        """
        answers = iter([[PUBLIC], ["127.0.0.1"], ["127.0.0.1"]])
        calls: list[str] = []

        def _resolve(host: str) -> tuple[Any, ...]:
            if host != "rebind.example.test":
                return ()
            calls.append(host)
            return tuple(ipaddress.ip_address(value) for value in next(answers, ["127.0.0.1"]))

        monkeypatch.setattr(outbound, "resolve_host", _resolve)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request("GET", "https://rebind.example.test/", client=transport(handler))

        assert seen[0].url.host == PUBLIC
        assert calls == ["rebind.example.test"], "the name must be resolved exactly once"


class TestRedirects:
    def _chain(self, targets: dict[str, str]) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            location = targets.get(request.url.path)
            if location:
                return httpx.Response(302, headers={"Location": location})
            return httpx.Response(200, json={"path": request.url.path})

        return handler

    def test_a_redirect_to_a_private_address_is_refused(self, monkeypatch: Any) -> None:
        """The bypass a first-URL-only check hands over for free."""
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = self._chain({"/start": "http://169.254.169.254/latest/meta-data/"})
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "https://api.example.test/start", client=transport(handler))

    def test_a_redirect_to_the_deployment_is_refused(self, monkeypatch: Any, settings: Any) -> None:
        settings.APP_URL = "https://chat.example.test"
        settings.ALLOWED_HOSTS = ["chat.example.test"]
        resolving({"api.example.test": [PUBLIC], "chat.example.test": [PUBLIC_ALT]}, monkeypatch)
        handler = self._chain({"/start": "https://chat.example.test/internal/tick"})
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", "https://api.example.test/start", client=transport(handler))

    def test_an_unparseable_location_is_a_refusal_not_a_crash(self, monkeypatch: Any) -> None:
        """``Location`` is written by the far end, which is a stranger's server."""
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = self._chain({"/start": "http://[oops"})
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "https://api.example.test/start", client=transport(handler))

    def test_a_javascript_location_is_refused(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = self._chain({"/start": "javascript:alert(1)"})
        with pytest.raises(BlockedURLError):
            guarded_request("GET", "https://api.example.test/start", client=transport(handler))

    def test_redirects_are_followed_up_to_the_cap(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = self._chain({"/0": "/1", "/1": "/2", "/2": "/3"})
        response = guarded_request("GET", "https://api.example.test/0", client=transport(handler))
        assert response.json() == {"path": "/3"}
        assert response.final_url == "https://api.example.test/3"

    def test_one_redirect_too_many_is_refused(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = self._chain({"/0": "/1", "/1": "/2", "/2": "/3", "/3": "/4"})
        with pytest.raises(BlockedURLError, match=f"more than {MAX_REDIRECTS}"):
            guarded_request("GET", "https://api.example.test/0", client=transport(handler))

    def test_credentials_are_dropped_when_the_origin_changes(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC], "cdn.example.test": [PUBLIC_ALT]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "https://cdn.example.test/blob"})
            return httpx.Response(200, json={})

        guarded_request(
            "GET",
            "https://api.example.test/start",
            headers={"Authorization": "Bearer shhh", "X-Trace": "keep-me"},
            client=transport(handler),
        )
        assert seen[0].headers.get("authorization") == "Bearer shhh"
        assert "authorization" not in seen[1].headers, "a redirect must not leak the token to another host"
        assert seen[1].headers["x-trace"] == "keep-me"

    def test_credentials_survive_a_same_origin_redirect(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/moved"})
            return httpx.Response(200, json={})

        guarded_request(
            "GET",
            "https://api.example.test/start",
            headers={"Authorization": "Bearer shhh"},
            client=transport(handler),
        )
        assert seen[1].headers.get("authorization") == "Bearer shhh"

    def test_a_303_becomes_a_get_without_a_body(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(303, headers={"Location": "/result"})
            return httpx.Response(200, json={})

        guarded_request("POST", "https://api.example.test/start", json={"a": 1}, client=transport(handler))
        assert [request.method for request in seen] == ["POST", "GET"]
        assert seen[1].read() == b""

    def test_a_307_keeps_the_method_and_the_body(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(307, headers={"Location": "/result"})
            return httpx.Response(200, json={})

        guarded_request("POST", "https://api.example.test/start", json={"a": 1}, client=transport(handler))
        assert [request.method for request in seen] == ["POST", "POST"]
        assert seen[1].read() == b'{"a":1}'


class TestPrivateRangesAllowed:
    """``EXTERNAL_REQUEST_ALLOW_PRIVATE`` and the exact size of the hole it opens."""

    @pytest.fixture(autouse=True)
    def _on_prem(self, settings: Any) -> None:
        settings.EXTERNAL_REQUEST_ALLOW_PRIVATE = True

    @pytest.mark.parametrize("address", ["10.0.0.1", "172.16.0.1", "192.168.1.1", "fd00::1", "100.64.0.1"])
    def test_private_ranges_become_reachable(self, address: str) -> None:
        host = f"[{address}]" if ":" in address else address
        assert guarded_request("GET", f"http://{host}/health", client=transport(ok())).ok

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0", "224.0.0.1"],  # noqa: S104 - see above
    )
    def test_it_flips_private_ranges_only(self, address: str) -> None:
        """Loopback and the metadata service are never an integration target."""
        host = f"[{address}]" if ":" in address else address
        with pytest.raises(BlockedURLError):
            guarded_request("GET", f"http://{host}/", client=transport(unreachable))

    @pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "2002:7f00:0001::", "::ffff:169.254.169.254"])
    def test_a_v6_wrapper_around_a_denied_v4_is_still_denied(self, address: str) -> None:
        """The case ``_unwrap`` exists for, and it only bites with the flag on.

        With the flag off these are refused anyway — Python calls every one of
        them ``is_private``. With it on, the not-globally-routable rule stops
        applying, and unwrapping the embedded ``127.0.0.1`` is the only thing
        left between an on-prem deployment and its own loopback interface.
        """
        with pytest.raises(BlockedURLError):
            guarded_request("GET", f"http://[{address}]/", client=transport(unreachable))

    def test_a_v6_wrapper_around_a_private_v4_is_reachable(self) -> None:
        """The other side of the same coin: on-prem means on-prem."""
        assert guarded_request("GET", "http://[::ffff:10.0.0.1]/health", client=transport(ok())).ok

    def test_the_deployments_own_host_stays_refused(self, monkeypatch: Any, settings: Any) -> None:
        settings.APP_URL = "http://10.0.0.5:8000"
        settings.ALLOWED_HOSTS = ["10.0.0.5"]
        resolving({"10.0.0.5": ["10.0.0.5"]}, monkeypatch)
        with pytest.raises(BlockedURLError, match="back at this deployment"):
            guarded_request("GET", "http://10.0.0.5:8000/internal/tick", client=transport(unreachable))


class TestResponseCap:
    def test_a_declared_oversize_body_is_never_read(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 50, headers={"Content-Length": "999999999"})

        response = guarded_request("GET", "https://api.example.test/big", max_bytes=1024, client=transport(handler))
        assert response.truncated is True
        assert response.content == b""

    def test_a_streamed_body_is_cut_off_at_the_cap(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)

        def chunks() -> Any:
            for _ in range(100):
                yield b"y" * 100

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=chunks())

        response = guarded_request("GET", "https://api.example.test/big", max_bytes=250, client=transport(handler))
        assert response.truncated is True
        assert len(response.content) == 250

    def test_a_body_under_the_cap_arrives_whole(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        response = guarded_request("GET", "https://api.example.test/", client=transport(ok({"id": "abc"})))
        assert response.truncated is False
        assert response.json() == {"id": "abc"}

    def test_the_default_cap_comes_from_settings(self, monkeypatch: Any, settings: Any) -> None:
        settings.EXTERNAL_REQUEST_MAX_RESPONSE_BYTES = 64
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)

        def chunks() -> Any:
            for _ in range(50):
                yield b"z" * 10

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=chunks())

        response = guarded_request("GET", "https://api.example.test/", client=transport(handler))
        assert len(response.content) == 64
        assert response.truncated is True


class TestHeaderHygiene:
    def test_a_header_carrying_a_newline_is_dropped(self, monkeypatch: Any) -> None:
        """Header splitting: "a\\r\\nX-Admin: 1" is two headers at the far end."""
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request(
            "GET",
            "https://api.example.test/",
            headers={"X-Evil": "a\r\nX-Admin: 1", "X-Fine": "ok"},
            client=transport(handler),
        )
        assert "x-evil" not in seen[0].headers
        assert "x-admin" not in seen[0].headers
        assert seen[0].headers["x-fine"] == "ok"

    def test_a_caller_cannot_override_host(self, monkeypatch: Any) -> None:
        """Overriding Host would point the pinned connection's virtual host elsewhere."""
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        guarded_request(
            "GET",
            "https://api.example.test/",
            headers={"Host": "admin.internal"},
            client=transport(handler),
        )
        assert seen[0].headers["host"] == "api.example.test"


class TestFailuresAndResults:
    def test_a_timeout_is_a_transport_error_naming_only_the_host(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        with pytest.raises(OutboundTransportError) as caught:
            guarded_request("GET", "https://api.example.test/v1?token=super-secret", client=transport(handler))
        assert "super-secret" not in str(caught.value), "SECURITY-BASELINE §5: a URL's query carries credentials"
        assert "api.example.test" in str(caught.value)

    def test_a_connection_error_names_only_the_host(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        with pytest.raises(OutboundTransportError):
            guarded_request("GET", "https://api.example.test/", client=transport(handler))

    def test_a_non_2xx_is_a_result_not_an_exception(self, monkeypatch: Any) -> None:
        """The caller decides what a 404 means; the guard only refuses addresses."""
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        handler = lambda request: httpx.Response(404, json={"error": "nope"})  # noqa: E731
        response = guarded_request("GET", "https://api.example.test/", client=transport(handler))
        assert response.status_code == 404
        assert response.ok is False

    def test_the_response_reports_where_it_ended_up(self, monkeypatch: Any) -> None:
        resolving({"api.example.test": [PUBLIC]}, monkeypatch)
        response = guarded_request("GET", "https://api.example.test/v1", client=transport(ok()))
        assert response.final_url == "https://api.example.test/v1", "the hostname, not the pinned address"
        assert response.elapsed_ms >= 0


class TestGuardedResponseDecoding:
    def _response(self, content: bytes, content_type: str) -> GuardedResponse:
        return GuardedResponse(
            status_code=200,
            headers=httpx.Headers({"content-type": content_type}),
            content=content,
            truncated=False,
            elapsed_ms=1,
            final_url="https://api.example.test/",
        )

    def test_a_declared_charset_is_honoured(self) -> None:
        assert self._response("café".encode("latin-1"), "text/plain; charset=latin-1").text == "café"

    def test_an_unknown_charset_falls_back_rather_than_raising(self) -> None:
        assert self._response(b"hi", "text/plain; charset=nonsense-9").text == "hi"

    def test_undecodable_bytes_do_not_raise(self) -> None:
        """This body is quoted into a log line and an admin column; it may not explode."""
        assert self._response(b"\xff\xfe\x00", "application/json").text
