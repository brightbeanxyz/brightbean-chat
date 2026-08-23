"""``/api/v1/docs`` — the human reference, rendered by this project's own shell.

Ninja ships a docs page that loads Swagger UI from a CDN. SECURITY-BASELINE §8
puts a nonce-based CSP on every page in this product, which blocks exactly that,
and vendoring Swagger UI would put a new npm dependency inside the audit job and
a new entry in the vendor-drift check for a page that is read a handful of times
per deployment. So the interactive console is not shipped, and what is shipped
is a server-rendered page plus ``/api/v1/openapi.json`` for anyone who wants to
point their own tooling at the machine-readable document.

**The endpoint table is generated from the OpenAPI schema**, not typed out. A
hand-written table is a table that is wrong within two releases; this one cannot
disagree with the routes, because it *is* the routes.

Public, deliberately. It describes the API's shape and reads nothing — no
workspace, no key, no database. Requiring a session to read the reference for a
credential-authenticated API only makes integrating harder.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse

from apps.api.auth import SCOPE_PERMISSIONS
from apps.api.delivery import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from apps.api.events import EVENT_LABELS, SUBSCRIBABLE_EVENTS
from apps.api.keys import TOKEN_PREFIX
from apps.api.pagination import DEFAULT_LIMIT, MAX_LIMIT
from apps.common.context_processors import navigation_context

__all__ = ["api_docs"]

#: The error envelope's ``code`` values, with what each one means. Written down
#: here because a caller branches on them: they are as much a part of the
#: contract as the endpoint list, and a code that is not documented is a code
#: nobody can handle.
ERROR_CODES: tuple[tuple[str, int, str], ...] = (
    ("unauthenticated", 401, "Missing, malformed, unknown or revoked API key."),
    ("forbidden", 403, "The key is valid but its scopes do not cover this endpoint."),
    ("not_found", 404, "No such object in this key's workspace. Also the answer for another workspace's id."),
    ("payload_too_large", 413, "The request body is over the size cap."),
    ("invalid_request", 422, "A field is missing, unknown or the wrong shape. See detail.fields."),
    ("invalid_cursor", 422, "The cursor was not one this API produced."),
    ("invalid_field_value", 422, "The value does not fit the custom field's type."),
    ("body_too_deep", 422, "The JSON body nests deeper than the limit."),
    ("compliance_denied", 422, "The compliance engine refused the send. detail.reason says why."),
    ("no_api_trigger", 422, "The flow has no enabled api trigger to fire."),
    ("flow_not_runnable", 422, "The flow has no published version, or no single entry node."),
    ("rate_limited", 429, "Over the per-key limit. Retry-After says how long to wait."),
    ("server_error", 500, "Something went wrong on our side. Nothing is echoed back."),
)


def _endpoints() -> list[dict[str, Any]]:
    """The route table, straight out of the generated OpenAPI document."""
    from apps.api.api import api

    schema = api.get_openapi_schema()
    rows: list[dict[str, Any]] = []
    for path, operations in sorted(schema.get("paths", {}).items()):
        for method, operation in sorted(operations.items()):
            rows.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": (operation.get("summary") or "").strip(),
                    "description": (operation.get("description") or "").strip().split("\n\n")[0],
                }
            )
    return rows


def api_docs(request: HttpRequest) -> HttpResponse:
    """Render the reference."""
    context = navigation_context(request)
    context.update(
        {
            "endpoints": _endpoints(),
            "openapi_url": reverse("api_v1:openapi-json"),
            "token_prefix": TOKEN_PREFIX,
            "scope_rows": [
                {"scope": scope, "permissions": ", ".join(sorted(permissions))}
                for scope, permissions in sorted(SCOPE_PERMISSIONS.items())
            ],
            "rate_limit": settings.API_RATE_LIMIT_PER_SECOND,
            "max_body_bytes": settings.API_MAX_BODY_BYTES,
            "default_limit": DEFAULT_LIMIT,
            "max_limit": MAX_LIMIT,
            "error_codes": ERROR_CODES,
            "event_rows": [{"name": name, "label": EVENT_LABELS.get(name, name)} for name in SUBSCRIBABLE_EVENTS],
            "signature_header": SIGNATURE_HEADER,
            "timestamp_header": TIMESTAMP_HEADER,
            "event_header": EVENT_HEADER,
            "delivery_header": DELIVERY_HEADER,
            "timestamp_tolerance": settings.API_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
            "failure_limit": settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES,
        }
    )
    return render(request, "api/docs.html", context)
