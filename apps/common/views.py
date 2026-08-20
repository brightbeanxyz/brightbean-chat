"""Views owned by the scaffold: the placeholder page and the health probe."""

import logging

from django.db import Error as DatabaseError
from django.db import connections
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render

logger = logging.getLogger(__name__)


def index(request: HttpRequest) -> HttpResponse:
    """Placeholder landing page. Issue #32 replaces this with the real shell."""
    return render(request, "index.html")


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe with a real database round-trip (SPEC §20).

    Studio's ``/health/`` returns a bare JSON ``ok`` and never touches the
    database, so it stays green while the app is entirely unable to serve
    traffic. This one actually asks Postgres a question.

    The failure body carries no exception detail: the endpoint is reachable
    unauthenticated (it is exempt from the production HTTPS redirect so
    in-cluster probes can hit it), and DB errors leak connection strings,
    hostnames and schema names.
    """
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except (DatabaseError, OSError) as exc:
        logger.error("Health check failed: database unreachable (%s)", type(exc).__name__)
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"})
