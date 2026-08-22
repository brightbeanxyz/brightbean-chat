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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock

import httpx

from apps.common.outbound import GUARD_EXTENSION

__all__ = ["UnguardedRequestError", "guard_required"]


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
