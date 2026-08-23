"""Deep links for ref-URL triggers, and the seam that supplies the handle.

SPEC §10's ref trigger fires when somebody arrives through
``t.me/<bot>?start=<ref>``, ``m.me/<page>?ref=<ref>`` or ``ig.me/m/<user>?ref=<ref>``.
Building one needs the account's **public handle**, and ``ChannelConnection``
does not have a column for it: ``external_id`` is documented as "Page id, IG user
id, WABA phone number id, bot id, Twilio number or sending domain", and a
Telegram bot id is not its ``@username``.

Adding a column would mean editing ``apps/channels``, which a same-layer sibling
owns, so the handle is resolved in three steps instead:

1. the trigger's own ``link_handle``, which the panel offers as a text field —
   so a self-hoster gets a working link and QR **today**, with no adapter;
2. a resolver an adapter registered from its own ``ready()``, which is how
   Telegram's ``getMe`` username or Instagram's account name gets here once
   L4-B and L5-A land, still with no edit to this module;
3. ``external_id``, which is already a valid handle for Messenger — ``m.me/<page-id>``
   is a real deep link — and is the reason Messenger works out of the box.

A connection with no handle yields a :class:`RefLink` carrying a reason rather
than a broken URL, and the panel renders the reason.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from apps.common.platforms import Platform

__all__ = [
    "REF_LINK_TEMPLATES",
    "RefLink",
    "register_handle_resolver",
    "covers",
    "ref_link",
    "ref_links_for",
    "registered_handle_resolvers",
]

logger = logging.getLogger(__name__)

#: The three platforms SPEC §10 gives a ref to, and their deep-link shapes.
#: ``{ref}`` is interpolated unencoded, which is safe because
#: ``apps.flows.triggers.types.REF_PATTERN`` restricts a ref to characters that
#: are already URL-safe — so the link and the bytes inside the QR are the same
#: string, and there is no encode/decode step to get wrong.
REF_LINK_TEMPLATES: dict[str, str] = {
    Platform.TELEGRAM: "https://t.me/{handle}?start={ref}",
    Platform.MESSENGER: "https://m.me/{handle}?ref={ref}",
    Platform.INSTAGRAM: "https://ig.me/m/{handle}?ref={ref}",
}

HandleResolver = Callable[[Any], str]

#: Messenger needs no resolver: its ``external_id`` *is* the page id, and
#: ``m.me/<page-id>`` resolves. The other two ship without one and say so.
_RESOLVERS: dict[str, HandleResolver] = {Platform.MESSENGER: lambda connection: connection.external_id or ""}


@dataclass(frozen=True)
class RefLink:
    """One connection's link for one trigger — or why there isn't one."""

    connection: Any
    platform: str
    url: str
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return bool(self.url)


def register_handle_resolver(platform: str, resolver: HandleResolver, *, replace: bool = False) -> None:
    """Teach this module how to find a platform's public handle.

    One line in an adapter's ``AppConfig.ready()``. It exists so that L4-B and
    L5-A can make Telegram and Instagram links work without either of them
    editing this file or ``apps/channels``.
    """
    if platform in _RESOLVERS and not replace:
        raise ValueError(f"{platform!r} already has a handle resolver.")
    _RESOLVERS[platform] = resolver


def registered_handle_resolvers() -> tuple[str, ...]:
    """Platforms that can resolve a handle on their own, sorted."""
    return tuple(sorted(_RESOLVERS))


def handle_for(connection: Any, trigger: Any = None) -> str:
    """This connection's public handle, by the three-step rule above.

    The trigger's ``link_handle`` names **one** account, so it is honoured only
    when the trigger names one too. An unbound ref trigger covers every
    connection of a matching platform — several Telegram bots, say — and letting
    a single typed username stand in for all of them would print QR codes
    pointing at the wrong account, which is the one failure mode a printed code
    cannot be corrected after.
    """
    if trigger is not None and str(getattr(trigger, "channel_connection_id", "") or "") == str(connection.pk):
        configured = (trigger.config_json or {}).get("link_handle")
        if isinstance(configured, str) and configured.strip():
            return configured.strip().lstrip("@")

    resolver = _RESOLVERS.get(connection.platform)
    if resolver is None:
        return ""
    try:
        handle = resolver(connection)
    except Exception:  # pragma: no cover - an adapter's resolver misbehaving
        logger.exception("Handle resolver for %s failed on connection %s", connection.platform, connection.pk)
        return ""
    return (handle or "").strip().lstrip("@")


def ref_link(connection: Any, ref: str, *, trigger: Any = None) -> RefLink:
    """The deep link for ``ref`` on this connection."""
    template = REF_LINK_TEMPLATES.get(connection.platform)
    if template is None:
        return RefLink(
            connection=connection,
            platform=connection.platform,
            url="",
            unavailable_reason="This channel does not support reference links.",
        )
    if not ref:
        return RefLink(connection, connection.platform, "", "This trigger has no reference yet.")

    handle = handle_for(connection, trigger)
    if not handle:
        return RefLink(
            connection=connection,
            platform=connection.platform,
            url="",
            unavailable_reason=(
                "This channel has not reported its public username yet. Enter it above, or reconnect the channel."
            ),
        )
    return RefLink(connection, connection.platform, template.format(handle=handle, ref=ref))


def covers(trigger: Any, connection: Any) -> bool:
    """Whether this trigger fires on this connection.

    The same rule :func:`~apps.flows.triggers.matching.eligible_triggers` applies
    at match time — a bound trigger covers its own connection, an unbound one
    covers every connection of a matching platform — so a link the panel offers
    and a link the matcher would honour cannot disagree.
    """
    from apps.flows.triggers.registry import spec_for

    if trigger.channel_connection_id is not None:
        return str(trigger.channel_connection_id) == str(connection.pk)
    spec = spec_for(trigger.type)
    return spec is not None and connection.platform in spec.platforms


def ref_links_for(trigger: Any) -> list[RefLink]:
    """Every link this trigger covers — one per connection it can fire on.

    A bound trigger has exactly one. An unbound one covers every connection of a
    matching platform, and those are genuinely different links to genuinely
    different accounts, which is why the QR endpoint is addressed by connection
    rather than by trigger.
    """
    from apps.flows.triggers.registry import spec_for

    ref = (trigger.config_json or {}).get("ref") or ""
    if trigger.channel_connection_id is not None:
        return [ref_link(trigger.channel_connection, ref, trigger=trigger)]

    spec = spec_for(trigger.type)
    if spec is None:
        return []
    return [ref_link(connection, ref, trigger=trigger) for connection in _covered_connections(trigger, spec.platforms)]


def _covered_connections(trigger: Any, platforms: frozenset[str]) -> list[Any]:
    from apps.flows.compat import installed_model

    model = installed_model("channels", "apps.channels", "ChannelConnection")
    if model is None:  # pragma: no cover - channels is always installed
        return []
    return list(
        model.objects.for_workspace(trigger.workspace_id)
        .filter(platform__in=sorted(platforms))
        .order_by("display_name")
    )
