"""The adapter/policy/capability registry — ROADMAP contract 4.

    each platform adds one module in ``channels/providers/`` and one registry
    entry ``platform -> (Adapter, PlatformPolicy)``. […] The static per-platform
    **Capabilities table** also lives in the registry so L2-D's validation can
    emit capability warnings without importing adapter code. […] Additive only.

The split that makes both halves of that sentence true: the **data** lives in
:mod:`apps.channels.policy` and :mod:`apps.channels.capabilities`, which import
nothing but the platform enum, while the **adapters** register themselves here.
:func:`entry_for` answers with policy and capabilities whether or not an adapter
exists, which is why L2-D can ship its capability warnings today with no adapter
merged anywhere in the repo.

Registration is additive and explicit. A Layer-4/5 adapter module ends with::

    register_adapter(Platform.TELEGRAM, TelegramAdapter)

and :meth:`ChannelsConfig.ready` imports ``apps.channels.providers`` so the call
runs once per process. Re-registering the same platform raises rather than
overwriting: two adapters for one platform is a merge accident, and the one that
wins would depend on import order.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.channels.capabilities import Capabilities, capabilities_for
from apps.channels.policy import PlatformPolicy, policy_for
from apps.common.platforms import Platform

if TYPE_CHECKING:
    from apps.channels.providers.base import Adapter

logger = logging.getLogger(__name__)

__all__ = [
    "CONNECT_ROUTES",
    "RegistryEntry",
    "adapter_for",
    "connect_route_for",
    "entry_for",
    "has_adapter",
    "register_adapter",
    "registered_platforms",
    "unregister_adapter",
]

#: Platforms with a guided connect flow, and the named route that serves it.
#:
#: Per-platform data like the policy and capability tables, and here rather than
#: in ``views`` because ``forms`` needs it too: a platform with a guided flow is
#: one the generic "add a channel" form must **refuse**, since that form creates
#: a row with no credentials and every send on it fails. Each Layer-5 adapter
#: adds its entry with its connect view.
CONNECT_ROUTES: dict[str, str] = {
    Platform.TELEGRAM.value: "channels:telegram_connect",
    Platform.SMS.value: "channels:sms_connect",
}


def connect_route_for(platform: str) -> str:
    """The guided connect route for ``platform``, or "" if it has none yet."""
    return CONNECT_ROUTES.get(platform, "")


_ADAPTERS: dict[str, type["Adapter"]] = {}


@dataclass(frozen=True)
class RegistryEntry:
    """Contract 4's tuple, named.

    ``adapter_cls`` is None until the platform's own issue lands. Everything
    else is available from day one — which is the point.
    """

    platform: str
    adapter_cls: "type[Adapter] | None"
    policy: PlatformPolicy
    capabilities: Capabilities


class AdapterNotRegisteredError(LookupError):
    """No adapter has been registered for this platform yet."""


def register_adapter(platform: str, adapter_cls: type["Adapter"]) -> None:
    """Register ``adapter_cls`` as the adapter for ``platform``.

    Raises on an unknown platform (the enum is the vocabulary — a typo here
    would create a channel nothing else in the system knows about) and on a
    duplicate registration.
    """
    if platform not in Platform.values:
        raise ValueError(
            f"Unknown platform {platform!r}. Add it to apps.common.platforms.Platform first — "
            f"that enum is the single source of truth (ROADMAP contract 4)."
        )
    existing = _ADAPTERS.get(platform)
    if existing is not None and existing is not adapter_cls:
        raise ValueError(
            f"{platform!r} already has an adapter registered ({existing.__module__}.{existing.__qualname__}). "
            f"Contract 4 is additive: one adapter per platform, and which one wins must not depend on "
            f"import order."
        )
    _ADAPTERS[platform] = adapter_cls
    logger.debug("Registered %s adapter %s", platform, adapter_cls.__qualname__)


def unregister_adapter(platform: str) -> None:
    """Remove a registration. For tests, which install a fake adapter."""
    _ADAPTERS.pop(platform, None)


def has_adapter(platform: str) -> bool:
    return platform in _ADAPTERS


def registered_platforms() -> tuple[str, ...]:
    """Platforms with a working adapter, in enum order."""
    return tuple(value for value in Platform.values if value in _ADAPTERS)


def entry_for(platform: str) -> RegistryEntry:
    """Policy, capabilities and (if any) adapter class for ``platform``.

    Never raises for a platform in the enum: the data tables cover all six, and
    ``adapter_cls`` is simply None where no adapter has landed.
    """
    return RegistryEntry(
        platform=platform,
        adapter_cls=_ADAPTERS.get(platform),
        policy=policy_for(platform),
        capabilities=capabilities_for(platform),
    )


def adapter_for(platform: str) -> "Adapter":
    """An adapter instance for ``platform``.

    Raises :class:`AdapterNotRegisteredError` rather than returning None, so a
    caller cannot accidentally treat "no adapter" as "nothing to do" — on the
    webhook path that would be a silently dropped delivery.
    """
    adapter_cls = _ADAPTERS.get(platform)
    if adapter_cls is None:
        raise AdapterNotRegisteredError(
            f"No adapter registered for {platform!r}. Telegram lands with issue #12 and the rest "
            f"with Layer 5; until then this platform can hold connections but cannot send or receive."
        )
    return adapter_cls()
