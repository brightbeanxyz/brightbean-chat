"""Per-platform adapters (SPEC §6.1).

Issue #4 built the framework and #12 (L4-B) shipped the first adapter, Telegram;
the remaining five arrive across Layer 5. Each adds exactly one module here plus
one ``register_adapter(...)`` line at its foot, which is the whole of ROADMAP
contract 4's additive promise.

``meta_common`` is the exception that proves it: it is a *shared helper* rather
than an adapter, holds no ``register_adapter`` call and is never listed below.
Instagram (#17) and Messenger (#18) are one API wearing two hats, and the half
they genuinely share lives there so neither has to import the other.

``apps.channels.apps.ChannelsConfig.ready`` imports this package so those
registration calls run once per process. A new adapter module therefore has to
be imported from here — add it to :data:`ADAPTER_MODULES` — or its registration
never runs and the platform silently has no adapter.
"""

from importlib import import_module

__all__ = ["ADAPTER_MODULES", "load_adapters"]

#: Adapter modules to import at startup, in order. Each is expected to call
#: ``apps.channels.registry.register_adapter`` on import.
ADAPTER_MODULES: tuple[str, ...] = ("telegram", "instagram", "messenger", "sms", "whatsapp")


def load_adapters() -> None:
    """Import every adapter module so it registers itself."""
    for name in ADAPTER_MODULES:
        import_module(f"{__name__}.{name}")
