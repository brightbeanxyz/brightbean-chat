"""Errors an adapter raises when a platform API says no.

Modelled on BrightBean Studio's ``providers/exceptions.py``. Two classes,
because the send pipeline treats them differently: an
:class:`APIError` fails the message, while a :class:`RateLimitError` reschedules
it — SPEC §6.2 says so explicitly ("on HTTP 429 honor ``retry_after`` and
reschedule"), and the token buckets in SPEC §8 exist to make it rare.

Neither carries the response body. A provider's error text routinely quotes the
request that caused it, including the access token in a query string
(SECURITY-BASELINE §5), and these exceptions end up in logs, on message rows and
in the inbox. ``status_code`` and the provider's own machine-readable ``code``
are what a human needs to look the failure up in the platform's documentation.
"""

__all__ = ["APIError", "AdapterError", "RateLimitError"]


class AdapterError(Exception):
    """Base class, so a caller can catch every adapter failure at once."""


class APIError(AdapterError):
    """A platform API rejected the request."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        #: The platform's own error code where it publishes one, e.g. Meta's
        #: ``error.code``. Empty when the platform gave nothing machine-readable.
        self.code = code

    def __str__(self) -> str:
        detail = ", ".join(
            part
            for part in (
                f"HTTP {self.status_code}" if self.status_code is not None else "",
                f"code={self.code}" if self.code else "",
            )
            if part
        )
        message = super().__str__()
        return f"{message} ({detail})" if detail else message


class RateLimitError(APIError):
    """The platform is throttling us.

    ``retry_after`` is in seconds and is None when the platform did not say —
    the caller then falls back to its own backoff rather than retrying
    immediately, which is what turns a throttle into an outage.
    """

    def __init__(
        self,
        message: str = "Rate limited",
        *,
        status_code: int | None = 429,
        code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, code=code)
        self.retry_after = retry_after
