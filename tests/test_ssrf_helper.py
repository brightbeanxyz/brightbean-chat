"""``tests/ssrf.py`` itself — the thing every later call site's proof rests on.

A helper that silently passes is worse than no helper: #25 and every media
fetch after it will cite `guard_required()` as evidence, so it needs its own
evidence. Two cases, and the second is the one that matters — a helper that
never fails proves nothing.
"""

import httpx
import pytest

from apps.common.outbound import guarded_request
from tests.ssrf import UnguardedRequestError, guard_required


def _mock_client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))


def guarded_caller() -> None:
    """A well-behaved call site."""
    guarded_request("GET", "https://93.184.216.34/v1", client=_mock_client())


def unguarded_caller() -> None:
    """The regression this exists to catch: httpx reached for directly."""
    _mock_client().get("https://93.184.216.34/v1")


class TestGuardRequired:
    def test_a_guarded_call_passes_and_is_recorded(self) -> None:
        with guard_required() as requests:
            guarded_caller()
        assert [request.url.host for request in requests] == ["93.184.216.34"]

    def test_an_unguarded_call_fails_the_test(self) -> None:
        with pytest.raises(UnguardedRequestError, match="guarded_request"), guard_required():
            unguarded_caller()

    def test_an_unguarded_call_beside_a_guarded_one_still_fails(self) -> None:
        """The case that patching ``guarded_request`` and counting calls misses."""
        with pytest.raises(UnguardedRequestError), guard_required():
            guarded_caller()
            unguarded_caller()

    def test_no_request_at_all_is_not_a_failure(self) -> None:
        """The helper reports what happened; asserting a call happened is the test's job."""
        with guard_required() as requests:
            pass
        assert requests == []

    def test_the_patch_is_removed_afterwards(self) -> None:
        with guard_required():
            pass
        # Un-patched, this is an ordinary call that reaches the mock transport.
        assert _mock_client().get("https://93.184.216.34/v1").status_code == 200
