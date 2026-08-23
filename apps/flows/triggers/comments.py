"""Who answers a claimed comment — the seam L5-A and L5-B plug into.

:func:`apps.flows.triggers.matching._match_comment` already names this module:
"What L5-A and L5-B add is a parser that fills ``payload.extra``, the public
reply, the like, and the post picker — not a second copy of this." This is where
that half attaches.

The division of labour it fixes:

*This layer* decides **whether** a comment is answered. The matcher checks post
scope, keywords and the top-level rule; :func:`apps.flows.triggers.stages._claim_comment`
takes SPEC §10's two durable guards — once per comment, and once per commenter
per post — through :class:`apps.flows.models.HandledComment`. Both are
platform-agnostic and neither is repeated in an adapter.

*A platform* decides **how**. Posting a public reply, opening a DM thread that
does not exist yet, and whatever else that platform's API affords are things
only its adapter can do, and they are called through :func:`respond` at the one
moment the answer is "yes, and the guard is now ours".

Registration is by platform, and a platform that registers nothing simply has no
comment automation — which is the honest state for one whose adapter has not
landed rather than an error.

**This module imports nothing from this project**, on purpose and for the same
reason :mod:`apps.flows.triggers.hooks` does not: adapters register from their
own import-time side effects, and an import back into the models or the engine
would make ``INSTALLED_APPS`` order matter.

--------------------------------------------------------------------------
``supports_like`` is data, not a template branch
--------------------------------------------------------------------------

SPEC §10 gives the comment trigger a ``like_comment`` switch, and whether it can
be honoured is a fact about the platform's API rather than about the trigger.
Meta's IG Comment reference exposes ``like_count`` as read-only and only two
write operations — ``hide`` and the ``replies`` edge — so Instagram cannot like a
comment at all, while the Facebook Graph API's page comments can. Carrying that
as a field lets the trigger form offer the option exactly when some platform the
trigger could fire on can deliver it, without anything in ``apps.flows`` naming a
platform.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "CommentResponder",
    "Responder",
    "like_supported_on",
    "picker_routes",
    "register_responder",
    "registered_platforms",
    "respond",
    "responder_for",
    "unregister_responder",
]

#: ``responder(context, trigger, handled_comment) -> None``.
#:
#: Called with the routing context for the comment event, the trigger that won
#: the match, and the freshly claimed :class:`apps.flows.models.HandledComment`
#: row — which carries the comment id, the post id, the commenter and the
#: deadline the private reply has to be sent inside.
type Responder = Callable[[Any, Any, Any], None]


@dataclass(frozen=True)
class CommentResponder:
    """One platform's answer to a claimed comment, plus what its API affords."""

    respond: Responder
    #: Whether this platform's API can like a comment at all (SPEC §10).
    supports_like: bool = False
    #: The named route serving that platform's post picker, or "" for none.
    #: A *name*, not a URL: reversing it needs the workspace id, which is the
    #: view's to supply, and this module deliberately imports nothing.
    picker_route: str = ""


_RESPONDERS: dict[str, CommentResponder] = {}


def register_responder(platform: str, responder: CommentResponder, *, replace: bool = False) -> None:
    """Register ``platform``'s comment responder. A duplicate raises.

    Refusal rather than replacement, matching
    :func:`apps.channels.registry.register_adapter`: two modules claiming one
    platform's comment handling is a merge accident, and which one wins must not
    depend on import order.
    """
    existing = _RESPONDERS.get(platform)
    if existing is not None and not replace and existing is not responder:
        raise ValueError(
            f"{platform!r} already has a comment responder. One per platform, and which one wins "
            f"must not depend on import order; pass replace=True if the override is deliberate."
        )
    _RESPONDERS[platform] = responder


def unregister_responder(platform: str) -> None:
    """Remove a registration. For tests, which install fakes."""
    _RESPONDERS.pop(platform, None)


def responder_for(platform: str) -> CommentResponder | None:
    """``platform``'s responder, or ``None`` where nothing registered one."""
    return _RESPONDERS.get(platform)


def registered_platforms() -> tuple[str, ...]:
    """Platforms with comment automation, sorted. For tests and for ops."""
    return tuple(sorted(_RESPONDERS))


def like_supported_on(platforms: Iterable[str]) -> bool:
    """Can *any* of these platforms like a comment?

    The trigger form asks this to decide whether to offer ``like_comment`` at
    all. An unbound trigger can fire on every platform SPEC §10 lists for the
    type, so the answer is a disjunction: the option appears while at least one
    of them could honour it, and disappears when none can.
    """
    return any(
        responder.supports_like for responder in (_RESPONDERS.get(platform) for platform in platforms) if responder
    )


def picker_routes(platforms: Iterable[str]) -> list[tuple[str, str]]:
    """``(platform, route name)`` for every platform with a post picker.

    A list rather than one value because an unbound comment trigger can fire on
    every platform SPEC §10 lists for the type, and each of those has its own
    posts to choose from. Sorted, so the drawer's buttons do not reorder
    themselves between requests.
    """
    found = [
        (platform, responder.picker_route)
        for platform, responder in ((item, _RESPONDERS.get(item)) for item in platforms)
        if responder is not None and responder.picker_route
    ]
    return sorted(found)


def respond(context: Any, trigger: Any, handled_comment: Any) -> None:
    """Hand a freshly claimed comment to its platform's responder.

    Failures are logged and swallowed. The claim is already taken and the
    routing stage is about to report the comment consumed; a responder that
    raised would otherwise propagate into
    :func:`apps.flows.triggers.hooks.run_stage`, which rolls back the savepoint
    the claim was written in — releasing the guard and letting the next delivery
    of the same comment claim it again.
    """
    platform = getattr(getattr(context, "connection", None), "platform", "")
    responder = _RESPONDERS.get(platform)
    if responder is None:
        logger.debug("No comment responder registered for %s; the claim stands with nothing to send.", platform)
        return
    try:
        responder.respond(context, trigger, handled_comment)
    except Exception:
        logger.exception("The %s comment responder failed for comment row %s.", platform, handled_comment.pk)
