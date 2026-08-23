"""One error shape for every failure on ``/api/v1/`` (SPEC §17, SECURITY-BASELINE §1).

Every non-2xx answer is the same document::

    {"error": {"code": "not_found", "message": "…", "detail": {}}}

``code`` is the machine-readable half and is stable; ``message`` is for a human
reading a log and may be reworded; ``detail`` carries structured extras — the
per-field list on a validation failure, the compliance reason on a refused send.

Two rules the handlers below exist to enforce:

**Nothing leaks.** No tracebacks, no exception text from an unexpected error, no
internal identifiers. A 500 says "Something went wrong." and the real detail
goes to the logs, where the scrubbing filter has already seen it.

**A missing object and a foreign object are the same answer.** Cross-workspace
access returns 404, never 403 (SECURITY-BASELINE §1) — over a UUID space, a 403
is the single piece of information an attacker was missing. 403 is still correct
for "your key is in this workspace but lacks the scope", which tells the caller
nothing they did not already know.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from ninja.errors import AuthenticationError, HttpError, ValidationError

LOG = logging.getLogger(__name__)

__all__ = [
    "ApiError",
    "PayloadTooLargeError",
    "RateLimitedError",
    "error_response",
    "register_exception_handlers",
]


class ApiError(Exception):
    """A failure a route raises deliberately, carrying its own shape."""

    status = 400
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.detail = detail


class RateLimitedError(ApiError):
    """SPEC §17's per-key limit was exceeded."""

    status = 429
    code = "rate_limited"

    def __init__(self, retry_after: int) -> None:
        super().__init__("Rate limit exceeded.", retry_after=retry_after)
        self.retry_after = retry_after


class PayloadTooLargeError(ApiError):
    """The request body is over ``API_MAX_BODY_BYTES`` (SECURITY-BASELINE §7)."""

    status = 413
    code = "payload_too_large"


def error_response(code: str, message: str, *, status: int, detail: Any = None) -> JsonResponse:
    """Build the envelope. The only place a non-2xx body is constructed."""
    body: dict[str, Any] = {"error": {"code": code, "message": message, "detail": detail or {}}}
    return JsonResponse(body, status=status)


def register_exception_handlers(api: Any) -> None:
    """Attach every handler to a ``NinjaAPI``.

    Ordered from most specific to least. Ninja looks handlers up by walking the
    exception's MRO, so the catch-all on ``Exception`` at the bottom only sees
    what nothing above claimed.
    """

    @api.exception_handler(ApiError)
    def _problem(request: Any, exc: ApiError) -> JsonResponse:
        response = error_response(exc.code, exc.message, status=exc.status, detail=exc.detail)
        if isinstance(exc, RateLimitedError):
            # SPEC §17's 429 contract. The window is one second wide, so this is
            # the true wait rather than a guess.
            response["Retry-After"] = str(exc.retry_after)
        return response

    @api.exception_handler(AuthenticationError)
    def _unauthenticated(request: Any, exc: AuthenticationError) -> JsonResponse:
        # Deliberately uniform: a missing header, a malformed token, an unknown
        # key, a revoked key and a key for a deleted workspace all land here
        # with the same body, so a caller learns nothing by probing.
        response = error_response(
            "unauthenticated",
            "A valid API key is required.",
            status=401,
        )
        response["WWW-Authenticate"] = 'Bearer realm="BrightBean Chat API"'
        return response

    @api.exception_handler(PermissionDenied)
    def _forbidden(request: Any, exc: PermissionDenied) -> JsonResponse:
        # No echo of the permission key: the caller knows their own scopes, and
        # the message is the same whichever gate refused.
        return error_response("forbidden", "This API key does not have access to that.", status=403)

    @api.exception_handler(Http404)
    def _not_found(request: Any, exc: Http404) -> JsonResponse:
        return error_response("not_found", "No such object.", status=404)

    @api.exception_handler(ValidationError)
    def _invalid(request: Any, exc: ValidationError) -> JsonResponse:
        return error_response(
            "invalid_request",
            "The request body or query string is not valid.",
            status=422,
            detail={"fields": exc.errors},
        )

    @api.exception_handler(HttpError)
    def _http_error(request: Any, exc: HttpError) -> JsonResponse:
        return error_response("http_error", str(exc), status=exc.status_code)

    @api.exception_handler(Exception)
    def _unexpected(request: Any, exc: Exception) -> JsonResponse:
        # Logged with the traceback, answered without it. The scrubbing filter
        # (apps.common.logging) is already on every handler.
        LOG.exception("Unhandled error on the public API: %s %s", request.method, request.path)
        return error_response("server_error", "Something went wrong.", status=500)
