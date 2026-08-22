"""Advisory per-platform media ceilings, checked at attach time.

Issue #16 asks for "MIME/size validation per platform ceilings ... warn, don't
block, since the target platform isn't fixed at upload". That last clause is the
whole design: a library asset has no destination. The same PNG may go to
Telegram today and WhatsApp tomorrow, so refusing it at upload would refuse it
for every platform on behalf of one. The warnings surface where a destination
*is* known — the picker, when a caller passes ``?platform=`` — and they never
block.

**Relationship to ROADMAP contract 4.** Issue #4 owns the per-platform
``Capabilities`` table, as registry data specifically so consumers can read it
without importing adapter code. This module reads it when it exists
(:func:`_registry_capabilities`) for the kind-support booleans — "does this
platform accept video at all" — and degrades to no kind warnings when it does
not, because #4 is a parallel sibling that may not have merged yet.

The byte ceilings below are **not** in contract 4: its ``Capabilities`` fields
are booleans plus ``max_buttons`` / ``max_quick_replies`` / ``max_text_len``,
with nothing about media size. So they live here, conservatively, and are
marked to be reconciled into the registry once #4 lands rather than duplicated
forever. They are advisory numbers on an advisory path; being a little strict
costs a warning nobody had to obey.
"""

from typing import Any

from apps.common.platforms import Platform
from apps.media_library.mimes import MediaKind

__all__ = ["warnings_for"]

_MB = 1024 * 1024

# platform -> kind -> max bytes. A kind absent from a platform's row is one the
# platform does not accept at all.
#
# TODO(#4): fold these into the contract-4 Capabilities registry once the
# channels app lands, so there is one table rather than two.
_CEILINGS: dict[str, dict[str, int]] = {
    Platform.TELEGRAM: {
        MediaKind.IMAGE: 10 * _MB,
        MediaKind.AUDIO: 50 * _MB,
        MediaKind.VIDEO: 50 * _MB,
        MediaKind.FILE: 50 * _MB,
    },
    Platform.INSTAGRAM: {
        MediaKind.IMAGE: 8 * _MB,
        MediaKind.AUDIO: 25 * _MB,
        MediaKind.VIDEO: 25 * _MB,
    },
    Platform.MESSENGER: {
        MediaKind.IMAGE: 25 * _MB,
        MediaKind.AUDIO: 25 * _MB,
        MediaKind.VIDEO: 25 * _MB,
        MediaKind.FILE: 25 * _MB,
    },
    Platform.WHATSAPP: {
        MediaKind.IMAGE: 5 * _MB,
        MediaKind.AUDIO: 16 * _MB,
        MediaKind.VIDEO: 16 * _MB,
        MediaKind.FILE: 100 * _MB,
    },
    Platform.SMS: {
        MediaKind.IMAGE: 5 * _MB,
        MediaKind.AUDIO: 5 * _MB,
        MediaKind.VIDEO: 5 * _MB,
    },
    Platform.EMAIL: {
        MediaKind.IMAGE: 25 * _MB,
        MediaKind.AUDIO: 25 * _MB,
        MediaKind.VIDEO: 25 * _MB,
        MediaKind.FILE: 25 * _MB,
    },
}


def _registry_capabilities(platform: str) -> Any:
    """Contract 4's static Capabilities row, or ``None`` before #4 merges.

    Imported lazily and defensively on purpose: this app must not grow a hard
    dependency on a sibling issue's module, and a caller of the picker must not
    get a 500 because the channels app is not installed yet.
    """
    try:
        from apps.channels.registry import capabilities_for  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        return capabilities_for(platform)
    except Exception:  # pragma: no cover - a registry miss is not our failure
        return None


def _kind_supported(platform: str, kind: str) -> bool | None:
    """``True``/``False`` from the registry, or ``None`` when it cannot say."""
    capabilities = _registry_capabilities(platform)
    if capabilities is None:
        return None
    supported = getattr(capabilities, kind, None)
    return bool(supported) if isinstance(supported, bool) else None


def warnings_for(*, platform: str, kind: str, size: int) -> list[str]:
    """Human-readable ceilings this asset would exceed on ``platform``.

    An empty list means "no known problem", which is also what an unknown
    platform returns — this path advises, so silence is the safe answer.
    """
    if platform not in _CEILINGS:
        return []

    messages: list[str] = []
    ceilings = _CEILINGS[platform]
    label = Platform(platform).label

    supported = _kind_supported(platform, kind)
    if supported is False or (supported is None and kind not in ceilings):
        return [f"{label} does not accept {kind} messages."]

    limit = ceilings.get(kind)
    if limit is not None and size > limit:
        messages.append(f"{label} accepts {kind} files up to {limit // _MB} MB; this one is {size / _MB:.1f} MB.")
    return messages
