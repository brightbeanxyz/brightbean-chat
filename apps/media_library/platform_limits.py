"""Advisory per-platform media ceilings, checked at attach time.

Issue #16 asks for "MIME/size validation per platform ceilings ... warn, don't
block, since the target platform isn't fixed at upload". That last clause is the
whole design: a library asset has no destination. The same PNG may go to
Telegram today and WhatsApp tomorrow, so refusing it at upload would refuse it
for every platform on behalf of one. The warnings surface where a destination
*is* known — the picker, when a caller passes ``?platform=`` — and they never
block.

**There is one table now, and it is contract 4's.** This module used to carry
its own ``_CEILINGS`` dict beside a ``TODO(#4): fold these into the contract-4
Capabilities registry``, because when it was written #4 had not landed and the
registry's ``Capabilities`` carried booleans plus button and text limits with
nothing about media size. #4 landed, and #19 (L5-C) added the byte fields, so
this module now asks the registry both halves of the question — *does this
platform take this kind at all* and *how big may it be* — instead of answering
the first from the registry and the second from a copy that could disagree with
it. The disagreement was not hypothetical: the old table gave SMS a 5 MB image
ceiling while the registry said SMS carries no images at all.

Reading the registry through :func:`_registry_capabilities` stays lazy and
defensive: this app must not grow a hard dependency on another, and a caller of
the picker must not get a 500 because the channels app is not installed. With no
registry to ask there are no ceilings and no kinds, so the honest answer is
silence — which is what an advisory path should say when it knows nothing.
"""

from typing import Any

from apps.common.platforms import Platform

__all__ = ["warnings_for"]

_MB = 1024 * 1024


def _registry_capabilities(platform: str) -> Any:
    """Contract 4's static Capabilities row, or ``None`` when it cannot be read.

    Imported lazily and defensively on purpose: this app must not grow a hard
    dependency on a sibling issue's module, and a caller of the picker must not
    get a 500 because the channels app is not installed. An unknown platform
    raises out of ``capabilities_for`` by design (a permissive default would
    read as "everything is supported"), and here that is simply "nothing to
    say".
    """
    try:
        from apps.channels.registry import capabilities_for  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return capabilities_for(platform)
    except Exception:
        return None


def warnings_for(*, platform: str, kind: str, size: int) -> list[str]:
    """Human-readable ceilings this asset would exceed on ``platform``.

    An empty list means "no known problem", which is also what an unknown
    platform returns — this path advises, so silence is the safe answer.
    """
    capabilities = _registry_capabilities(platform)
    if capabilities is None:
        return []

    label = _label(platform)
    if not capabilities.supports_block(kind):
        return [f"{label} does not accept {kind} messages."]

    # 0 means the platform publishes no ceiling for this kind, not a ceiling of
    # zero. Warning on every file would make the picker useless.
    limit = capabilities.max_bytes_for(kind)
    if limit and size > limit:
        return [f"{label} accepts {kind} files up to {limit // _MB} MB; this one is {size / _MB:.1f} MB."]
    return []


def _label(platform: str) -> str:
    """The platform's display name, or its raw value if the enum has no such member."""
    try:
        return Platform(platform).label
    except ValueError:  # pragma: no cover - unreachable while the registry gates above
        return platform
