"""Recent posts on a connected account, for SPEC §10's comment-trigger picker.

A comment trigger can be scoped to specific posts (``post_scope: "specific"``,
``post_ids: [...]``), and until this module existed the only way to fill that in
was to paste ids into a textarea — ids that live nowhere a person can see them
without opening Meta's own tools.

**A registry, not a switch.** The two platforms that deliver comment events
(Instagram and Messenger, per ``apps.flows.triggers.types.PLATFORMS_FOR_TYPE``)
list posts through different Graph edges, and each one's adapter registers its
lister the way it registers the adapter itself. The picker view then knows only
that *some* platforms can list posts and that the rest cannot — which is the same
shape ``apps.channels.registry`` uses for adapters and the reason a second
platform costs one line.

**This module imports no adapter code and touches no database**, deliberately,
following ``apps.channels.capabilities``: the view that renders the picker lives
in ``apps.flows`` and must be able to ask "can this platform list posts?" without
dragging ``providers/`` into the flow builder's import graph.

Everything a lister returns is **attacker-controlled** — a page's post text is
written by whoever runs the page, and its permalink by Meta — so it is escaped on
render like any other platform string (SECURITY-BASELINE §2).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_POST_LIMIT",
    "MAX_POST_LIMIT",
    "Post",
    "PostListingError",
    "clean_post",
    "list_posts",
    "post_lister_for",
    "register_post_lister",
    "supports_post_listing",
]

logger = logging.getLogger(__name__)

#: How many posts the picker shows by default. Enough to cover "the campaign I
#: launched this week" without turning one panel open into a paginated crawl of a
#: page's whole history.
DEFAULT_POST_LIMIT = 25

#: The ceiling a caller may ask for. The limit reaches a Graph query string, so
#: it is clamped rather than trusted — a request for a million posts is a way to
#: make this deployment hammer Meta on someone else's behalf.
MAX_POST_LIMIT = 50

#: Bounds on what we keep from a listing. The message is a preview, not the post.
MAX_POST_TITLE_CHARS = 200
MAX_POST_URL_CHARS = 500


@dataclass(frozen=True)
class Post:
    """One post a comment trigger could be scoped to.

    Frozen and platform-neutral: an id, something to recognise it by, and
    somewhere to go and look at it. Anything richer would be a per-platform shape
    the template would have to branch on.
    """

    #: The id a ``post_ids`` entry holds, and what a comment event's
    #: ``COMMENT_POST_ID_KEY`` extra is matched against.
    id: str
    #: A short preview of the post's own text, or "" for a post with none.
    title: str = ""
    #: Where to view it. Empty when the platform did not give one; never fetched
    #: server-side, only rendered as a link.
    permalink: str = ""
    #: ISO-8601 as the platform sent it, for display order. Not parsed: it is
    #: shown, not compared, and parsing an attacker-adjacent timestamp to sort a
    #: list is more surface than the feature is worth.
    created_time: str = ""


class PostListingError(RuntimeError):
    """The platform would not list this account's posts.

    Distinct from an empty list, which means the account genuinely has no posts.
    The picker shows a different thing for each, because "reconnect this channel"
    and "publish something first" are different instructions.
    """


#: ``platform -> callable(connection, limit) -> list[Post]``.
Lister = Callable[[Any, int], list[Post]]

_LISTERS: dict[str, Lister] = {}


def register_post_lister(platform: str, lister: Lister, *, replace: bool = False) -> Lister:
    """Register how ``platform`` lists an account's recent posts.

    Duplicates raise, for the reason ``registry.register_adapter`` gives: two
    listers for one platform is a merge accident, and which one wins must not
    depend on import order.
    """
    existing = _LISTERS.get(platform)
    if existing is not None and not replace and existing is not lister:
        raise ValueError(
            f"{platform!r} already has a post lister registered "
            f"({existing.__module__}.{getattr(existing, '__qualname__', existing)}). "
            f"Pass replace=True if the override is deliberate."
        )
    _LISTERS[platform] = lister
    return lister


def post_lister_for(platform: str) -> Lister | None:
    """The lister for ``platform``, or None where the platform has none."""
    return _LISTERS.get(platform)


def supports_post_listing(platform: str) -> bool:
    """Whether a picker can be offered for ``platform`` at all."""
    return platform in _LISTERS


def list_posts(connection: Any, *, limit: int = DEFAULT_POST_LIMIT) -> list[Post]:
    """Recent posts on ``connection``'s account.

    Raises :class:`PostListingError` when the platform refuses, and returns an
    empty list when it answers with nothing. A platform with no lister registered
    raises too: the caller has already asked :func:`supports_post_listing`, so
    reaching here without one is a bug rather than an empty state.
    """
    lister = _LISTERS.get(getattr(connection, "platform", ""))
    if lister is None:
        raise PostListingError(f"No post lister is registered for {getattr(connection, 'platform', '')!r}.")
    bounded = max(1, min(int(limit), MAX_POST_LIMIT))
    return lister(connection, bounded)


def clean_post(
    *,
    post_id: Any,
    title: Any,
    permalink: Any,
    created_time: Any,
) -> Post | None:
    """Build a :class:`Post` from raw platform json, or None if it has no id.

    Shared by every lister so the bounds are applied once. Each field is
    type-checked rather than assumed, and NUL is stripped because these strings
    are rendered into a template and may be echoed back into a form value.
    """
    identifier = _clean(post_id, MAX_POST_TITLE_CHARS)
    if not identifier:
        return None
    return Post(
        id=identifier,
        title=_clean(title, MAX_POST_TITLE_CHARS),
        permalink=_clean(permalink, MAX_POST_URL_CHARS),
        created_time=_clean(created_time, 40),
    )


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]
