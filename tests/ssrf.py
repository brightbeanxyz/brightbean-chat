"""Proving a call site goes through the SSRF guard — SECURITY-BASELINE §6.

    No exceptions; new call sites add a test proving the guard is in the path.

"Proving" is the operative word. The obvious test — patch
``guarded_request`` and assert it was called — proves only that *a* call was
made through it, and stays green when the code makes a second, unguarded
request beside it. That is the failure this helper is built to catch, because
it is the shape a regression actually takes: someone adds a HEAD request to
sniff a content type, or a retry that reaches for ``httpx.get``, and the
existing assertion never notices.

So the check runs at the other end. ``guarded_request`` stamps every request it
issues with :data:`~apps.common.outbound.GUARD_EXTENSION`, and
:func:`guard_required` patches ``httpx.Client.send`` — the single funnel every
httpx request passes through, whatever transport is under it — to refuse
anything without that stamp::

    from tests.ssrf import guard_required

    def test_delivery_goes_through_the_guard():
        with guard_required() as requests:
            deliver_webhook(subscription, payload)
        assert [request.url.host for request in requests] == ["203.0.113.10"]

``httpx.Client.send`` rather than the transport, deliberately: a test that
injects a ``MockTransport`` never reaches ``HTTPTransport``, so patching there
would silently exempt exactly the tests most likely to be written.

This lives in ``tests/`` rather than in an app because its consumers are spread
across apps — the External Request node (#15), outbound webhooks (#25), media
fetch-by-URL — which is the same reason ``tests/idor.py`` lives here.
"""

import ipaddress
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any
from unittest import mock

import httpx

from apps.common import outbound
from apps.common.outbound import GUARD_EXTENSION, reset_deployment_cache

__all__ = [
    "FakeInternet",
    "UnguardedRequestError",
    "deployment_cache_cleared",
    "guard_required",
    "serving",
]


class UnguardedRequestError(AssertionError):
    """An HTTP request was made without going through ``guarded_request``.

    An ``AssertionError`` because that is what it is: the test asserted the
    guard was in the path, and this is the failure of that assertion, reported
    from where the violation happened rather than from a mock's call count.
    """


@contextmanager
def guard_required() -> Iterator[list[httpx.Request]]:
    """Fail any HTTP request inside this block that skipped the SSRF guard.

    Yields the list of guarded requests, appended to as they are made, so a
    test can go on to assert *what* was requested — the pinned address, the
    ``Host`` header, the method.

    Both the sync and async clients are covered. Nothing in this project is
    async today; leaving ``AsyncClient`` open would make "port this to async"
    the way a call site silently loses its guard.
    """
    seen: list[httpx.Request] = []
    real_send = httpx.Client.send
    real_async_send = httpx.AsyncClient.send

    def _check(request: httpx.Request) -> None:
        if not request.extensions.get(GUARD_EXTENSION):
            raise UnguardedRequestError(
                f"{request.method} {request.url.scheme}://{request.url.host} was requested without going through "
                f"apps.common.outbound.guarded_request. SECURITY-BASELINE §6 allows no exceptions: a server-side "
                f"fetch of a user-supplied URL must use the guard, and one built from constants and stored ids "
                f"should use apps.channels.providers.base.request_json."
            )
        seen.append(request)

    def _send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        _check(request)
        return real_send(self, request, **kwargs)

    async def _async_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        _check(request)
        return await real_async_send(self, request, **kwargs)

    with (
        mock.patch.object(httpx.Client, "send", _send),
        mock.patch.object(httpx.AsyncClient, "send", _async_send),
    ):
        yield seen


# ---------------------------------------------------------------------------
# Driving a guarded call site without a socket
# ---------------------------------------------------------------------------


class FakeInternet:
    """DNS and the socket, replaced — with the guard left entirely real.

    The companion to :func:`guard_required`: that one proves a call site went
    through the guard, this one lets the guard actually run without leaving the
    process. Together they are what "a test proving the guard is in the path"
    means for a call site that has to *succeed* as well as be guarded.

    It patches :func:`apps.common.outbound.resolve_host` and
    ``httpx.HTTPTransport.handle_request`` rather than handing the caller an
    ``httpx.MockTransport``, because ``guarded_request`` builds its own client
    from a URL — a caller cannot inject a transport, and that is the shape
    production runs in. Patching the transport class also means the guard's real
    work happens: the scheme check, the address validation, the IP pinning, the
    redirect re-validation and the streaming cap all run against the canned
    response.

    ``names`` maps hostname to the addresses it resolves to. The default is
    empty, so a test that forgets to name a host gets a refusal from the guard
    rather than a confusing miss. Use a **globally routable** address in it:
    the documentation ranges (``192.0.2.0/24``, ``198.51.100.0/24``,
    ``203.0.113.0/24``) are reserved, the guard refuses reserved space, and a
    test using one fails for a reason that has nothing to do with its subject.

    Lives here rather than in an app's test package because its consumers are
    spread across apps, for the same reason :func:`guard_required` does. Three
    private copies existed before this one and had already started to diverge.
    """

    #: A genuinely global address, for tests that just need one that resolves.
    PUBLIC = "93.184.216.34"

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        names: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self.handler = handler
        self.names = dict(names or {})
        #: Every request that reached the transport, in order.
        self.requests: list[httpx.Request] = []

    def install(self, monkeypatch: Any) -> "FakeInternet":
        """Patch DNS and the transport for the duration of the test.

        Returns ``self`` so the common case is one line::

            internet = FakeInternet(serving(b"...")).install(monkeypatch)
        """
        monkeypatch.setattr(outbound, "resolve_host", self._resolve)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", self._handle)
        return self

    def _resolve(self, host: str) -> tuple[Any, ...]:
        return tuple(ipaddress.ip_address(value) for value in self.names.get(host.lower(), ()))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # Patched onto the class as an already-bound method, so ``self`` here is
        # the FakeInternet and the transport instance never arrives — which is
        # fine, since a canned response does not need one. Writing this as a
        # plain function instead is the mistake that costs an afternoon: it
        # becomes a method of the transport and the signature no longer matches.
        self.requests.append(request)
        return self.handler(request)


def serving(body: Any = None, *, status: int = 200, **kwargs: Any) -> Callable[[httpx.Request], httpx.Response]:
    """A handler for :class:`FakeInternet` that answers everything the same way.

    ``body`` is dispatched on its type, because the two kinds of call site want
    different things and neither should have to say so: ``bytes`` is a body
    served verbatim (a media fetch, where the *bytes* are the subject), and
    anything else is serialised as JSON (an API call, where the shape is). None
    serves an empty body.
    """

    def _handler(_request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status, **kwargs)
        if isinstance(body, bytes | bytearray):
            return httpx.Response(status, content=bytes(body), **kwargs)
        return httpx.Response(status, json=body, **kwargs)

    return _handler


@contextmanager
def deployment_cache_cleared() -> Iterator[None]:
    """Drop the guard's cache of this deployment's own addresses, both ways.

    The guard resolves ``APP_URL``'s host once and caches it, so a test that
    swaps the resolver has to clear the cache before *and* after: before, or the
    real answer is still there; after, or the fake one leaks into the next test.
    """
    reset_deployment_cache()
    try:
        yield
    finally:
        reset_deployment_cache()
