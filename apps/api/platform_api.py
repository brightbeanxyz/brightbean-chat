"""The ``/api/platform/v1/`` Ninja instance.

A second ``NinjaAPI`` rather than more routers on ``api_v1``: that one is
authenticated by workspace-scoped keys and every operation reads
``request.workspace``, so mixing in routes that authenticate an installation
would make "which credential reaches this?" a per-route question. Here the
answer is the one ``auth=`` below.

``openapi_url=None``: ``/api/v1/openapi.json`` is public because it describes an
API anyone with a workspace may use; this one provisions tenants and its shape
is not for anonymous readers. The reference is ``docs/api/platform-v1.md``.
"""

from ninja import NinjaAPI

from apps.api.auth import PlatformKeyAuth
from apps.api.errors import register_exception_handlers
from apps.api.routers import platform

__all__ = ["platform_api"]

platform_api = NinjaAPI(
    title="BrightBean Chat Platform API",
    version="1.0.0",
    auth=PlatformKeyAuth(),
    docs_url=None,
    openapi_url=None,
    urls_namespace="platform_v1",
)

register_exception_handlers(platform_api)

platform_api.add_router("", platform.router)
