"""``resolve(media_id)`` — the send path's entry point into the library.

SPEC §11.1 lets a ``send_message`` block carry "image/audio/video/file by media
library id **or** URL". This is what turns the first form into the second, at
send time, so a block stores a stable id and the URL is minted fresh from
whatever storage backend the deployment is running.

Called from a worker, not a request: there is no session, no ``request.user``
and no middleware-resolved workspace. Hence the required ``workspace`` keyword —
issue #16 writes the signature as ``resolve(media_id)``, but an id-only lookup
on a tenant model is precisely the hole ``WorkspaceScopedModel`` exists to
close, and a flow execution always knows which workspace it belongs to. Passing
it costs the caller nothing and makes a cross-tenant media id a miss rather than
a leak.

Failure is one exception, :class:`MediaNotFoundError`, for both "no such
asset" and "that asset belongs to someone else". The send path maps it to a
failed message and follows the ``default`` edge onward per SPEC §9.5 — a
deleted image stops the message, not the flow.
"""

from typing import Any
from uuid import UUID

__all__ = ["MediaNotFoundError", "resolve"]


class MediaNotFoundError(LookupError):
    """No asset with that id is readable from this workspace.

    Deliberately one exception for both causes. A caller that could tell "does
    not exist" from "belongs to another workspace" would be an existence oracle
    wearing a different hat, and the send path has no use for the distinction.
    """


def resolve(media_id: Any, *, workspace: Any) -> dict[str, str]:
    """Return ``{"url", "mime", "kind"}`` for one library asset.

    ``url`` is an absolute, signed delivery URL (see
    :mod:`apps.media_library.delivery`) — unguessable, long-lived, and valid
    only while the asset exists.
    """
    from apps.media_library.delivery import delivery_url
    from apps.media_library.models import MediaAsset

    try:
        pk = media_id if isinstance(media_id, UUID) else UUID(str(media_id))
    except (ValueError, AttributeError, TypeError) as exc:
        # A malformed id is a miss, not a crash: block config is user-authored.
        raise MediaNotFoundError(str(media_id)) from exc

    asset = MediaAsset.objects.for_workspace(workspace).filter(pk=pk).first()
    if asset is None:
        raise MediaNotFoundError(str(media_id))

    return {
        "url": delivery_url(asset),
        "mime": asset.mime,
        "kind": asset.kind,
    }
