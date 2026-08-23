"""Per-platform adapters (SPEC §6.1).

Issue #4 built the framework and #12 (L4-B) shipped the first adapter, Telegram;
the remaining five arrive across Layer 5. Each adds exactly one module here plus
one ``register_adapter(...)`` line at its foot, which is the whole of ROADMAP
contract 4's additive promise.

``apps.channels.apps.ChannelsConfig.ready`` imports this package so those
registration calls run once per process. A new adapter module therefore has to
be imported from here — add it to :data:`ADAPTER_MODULES` — or its registration
never runs and the platform silently has no adapter.
"""

from importlib import import_module

__all__ = ["ADAPTER_MODULES", "load_adapters"]

#: Adapter modules to import at startup, in order. Each is expected to call
#: ``apps.channels.registry.register_adapter`` on import.
ADAPTER_MODULES: tuple[str, ...] = ("telegram", "instagram", "sms")


def load_adapters() -> None:
    """Import every adapter module so it registers itself."""
    for name in ADAPTER_MODULES:
        import_module(f"{__name__}.{name}")
