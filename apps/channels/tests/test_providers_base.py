"""The shared HTTP transport in ``providers/base.py``.

No adapter ships in this layer, so nothing here talks to a platform — every test
runs against ``httpx.MockTransport``, which is also what a Layer-5 adapter's
tests will use. What is being pinned down is the *policy*: which failures become
which exception, and what a caller can rely on ``retry_after`` meaning.
"""

from typing import Any

import httpx
import pytest

from apps.channels.providers.base import CONNECT_TIMEOUT, READ_TIMEOUT, request_json
from apps.channels.providers.exceptions import APIError, RateLimitError

URL = "https://api.example.test/v1/send?access_token=super-secret-token"


def client_returning(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestSuccess:
    def test_a_json_object_is_returned(self) -> None:
        client = client_returning(lambda request: httpx.Response(200, json={"message_id": "m1"}))
        assert request_json("POST", URL, client=client) == {"message_id": "m1"}

    def test_a_non_object_body_is_an_error(self) -> None:
        client = client_returning(lambda request: httpx.Response(200, json=[1, 2, 3]))
        with pytest.raises(APIError, match="expected an object"):
            request_json("GET", URL, client=client)

    def test_a_non_json_body_is_an_error(self) -> None:
        client = client_returning(lambda request: httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(APIError, match="non-JSON"):
            request_json("GET", URL, client=client)


class TestRateLimiting:
    def test_429_becomes_a_rate_limit_error_with_retry_after(self) -> None:
        client = client_returning(lambda request: httpx.Response(429, headers={"Retry-After": "12"}, json={}))
        with pytest.raises(RateLimitError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.retry_after == 12.0

    def test_a_missing_retry_after_is_none_rather_than_zero(self) -> None:
        """Zero would mean "retry immediately", which turns a throttle into an outage."""
        client = client_returning(lambda request: httpx.Response(429, json={}))
        with pytest.raises(RateLimitError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.retry_after is None

    @pytest.mark.parametrize("value", ["soon", "", "Wed, 21 Oct 2015 07:28:00 GMT", "-5"])
    def test_an_unusable_retry_after_is_none(self, value: str) -> None:
        client = client_returning(lambda request: httpx.Response(429, headers={"Retry-After": value}, json={}))
        with pytest.raises(RateLimitError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.retry_after is None

    def test_a_rate_limit_error_is_also_an_api_error(self) -> None:
        # So a caller that only cares about "the send failed" catches one thing.
        assert issubclass(RateLimitError, APIError)


class TestErrors:
    def test_a_4xx_carries_the_status_and_the_platform_error_code(self) -> None:
        client = client_returning(lambda request: httpx.Response(400, json={"error": {"code": 190}}))
        with pytest.raises(APIError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.status_code == 400
        assert caught.value.code == "190"

    def test_telegrams_error_code_shape_is_understood_too(self) -> None:
        client = client_returning(lambda request: httpx.Response(403, json={"error_code": 403, "ok": False}))
        with pytest.raises(APIError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.code == "403"

    def test_an_unparseable_error_body_still_raises_cleanly(self) -> None:
        """Decoration on an error path must never raise on top of the failure."""
        client = client_returning(lambda request: httpx.Response(500, text="upstream exploded"))
        with pytest.raises(APIError) as caught:
            request_json("POST", URL, client=client)
        assert caught.value.status_code == 500
        assert caught.value.code == ""

    def test_a_timeout_becomes_an_api_error(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(APIError, match="timed out"):
            request_json("POST", URL, client=client_returning(timeout))

    def test_a_transport_failure_becomes_an_api_error(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(APIError, match="ConnectError"):
            request_json("POST", URL, client=client_returning(refused))


class TestNoCredentialLeaks:
    """SECURITY-BASELINE §5: these messages reach logs, message rows and the inbox."""

    @pytest.mark.parametrize(
        "handler",
        [
            lambda request: httpx.Response(400, json={"error": {"message": "bad token"}}),
            lambda request: httpx.Response(429, json={}),
            lambda request: httpx.Response(200, text="not json"),
        ],
    )
    def test_the_query_string_never_reaches_the_message(self, handler: Any) -> None:
        with pytest.raises(APIError) as caught:
            request_json("POST", URL, client=client_returning(handler))
        assert "super-secret-token" not in str(caught.value)
        assert "api.example.test" in str(caught.value)

    def test_a_transport_error_does_not_echo_the_url(self) -> None:
        # httpx puts the full URL into transport error strings, so the exception
        # type is reported rather than its message.
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect to {URL}", request=request)

        with pytest.raises(APIError) as caught:
            request_json("POST", URL, client=client_returning(refused))
        assert "super-secret-token" not in str(caught.value)

    def test_a_malformed_url_does_not_crash_the_error_path(self) -> None:
        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with pytest.raises(APIError, match="the platform|localhost"):
            request_json("POST", "http://localhost", client=client_returning(refused))


class TestTimeouts:
    def test_the_default_budget_matches_spec_7_1(self) -> None:
        """SPEC §7.1: "2 s hard timeout on the HTTP client" for the inline path."""
        assert READ_TIMEOUT == 2.0
        assert CONNECT_TIMEOUT <= READ_TIMEOUT

    def test_a_caller_can_override_the_timeout(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["extensions"] = request.extensions
            return httpx.Response(200, json={})

        request_json("POST", URL, client=client_returning(handler), timeout=30.0)
        assert seen["extensions"]["timeout"]["read"] == 30.0
