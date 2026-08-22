"""The media library (issue #16).

The send path's entry point is :func:`apps.media_library.resolve`, exposed here
so callers can write ``media_library.resolve(...)`` as the issue specifies:

    from apps import media_library

    media = media_library.resolve(block["media_id"], workspace=execution.workspace)

It is re-exported through a module ``__getattr__`` (PEP 562) rather than a plain
``from .resolution import resolve``. A Django app package is imported while the
app registry is still being populated, so a top-level import of anything that
touches models raises ``AppRegistryNotReady``; the lazy form defers that to the
first attribute access, which by definition happens after startup.
"""

from typing import Any

__all__ = ["MediaNotFoundError", "resolve"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from apps.media_library import resolution

        return getattr(resolution, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
