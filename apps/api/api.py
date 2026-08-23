"""The ``/api/v1/`` Ninja instance (SPEC §17).

One ``NinjaAPI`` with one authentication class, mounted by ``config/urls.py``.
Everything below is a deliberate departure from Ninja's defaults:

``auth=ApiKeyAuth()`` at the API level
    Global rather than per-operation. A route that forgets its ``auth=`` is an
    anonymous route, and the issue's requirement is "no anonymous surface" —
    so the default is authenticated and there is no opt-out.

``docs_url=None``
    Ninja's built-in docs page loads Swagger UI from a CDN, which
    SECURITY-BASELINE §8's nonce-based CSP blocks outright. ``/api/v1/docs`` is
    a server-rendered page in this project's own design system
    (:mod:`apps.api.views_docs`), and the machine-readable document is at
    ``/api/v1/openapi.json`` for anyone who wants to point their own tooling at
    it. The OpenAPI document describes the API's shape and carries no tenant
    data, so it is served without a key.

``urls_namespace="api_v1"``
    Fixed rather than derived. Every operation also names its own ``url_name``,
    so a route reverses as ``api_v1:contacts_list`` — stable strings that
    ``tests/idor.py``'s waiver list and ``apps/api/tests/test_isolation.py``
    both key off. Letting Ninja derive names from function names would make a
    rename a silent change to the waiver list.

``csrf`` is left at its default of ``False``
    Correct here and nowhere else in the project: CSRF defends a *cookie*
    credential, and this API refuses to look at one. Nothing in these routes
    authenticates from a session, so there is no ambient authority for a
    cross-site form post to borrow.
"""

from __future__ import annotations

from ninja import NinjaAPI

from apps.api.auth import ApiKeyAuth
from apps.api.errors import register_exception_handlers
from apps.api.routers import catalog, contacts, messages

__all__ = ["api"]

DESCRIPTION = """
The BrightBean Chat public API. Authenticate with an API key issued from
**Settings → Organization → API Keys**:

    Authorization: Bearer bb_...

Every key is scoped to one workspace; there is no cross-workspace call. Rate
limit is 10 requests per second per key. See `/api/v1/docs` for the full
reference, including outbound-webhook signature verification.
""".strip()

api = NinjaAPI(
    title="BrightBean Chat API",
    version="1.0.0",
    description=DESCRIPTION,
    auth=ApiKeyAuth(),
    docs_url=None,
    openapi_url="/openapi.json",
    urls_namespace="api_v1",
)

register_exception_handlers(api)

api.add_router("", contacts.router)
api.add_router("", messages.router)
api.add_router("", catalog.router)
