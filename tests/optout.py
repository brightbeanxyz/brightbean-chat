"""A tripwire at the adapter boundary, for SPEC §19's last line.

    Opt-out is enforced in the compliance engine, not in flows, so it cannot
    be bypassed.

"Cannot be bypassed" is a claim about **every** path, and the compliance suite
does not make it: ``apps/messaging/tests/test_compliance.py`` tests ``can_send``,
which is the *decision*. A decision nobody consults is not a control. What has
to be true is that no code path reaches an adapter for an opted-out identity,
and the place to assert that is the adapter.

--------------------------------------------------------------------------
Why the tripwire raises ``BaseException``
--------------------------------------------------------------------------

This is the detail the whole harness turns on. ``apps/messaging/services.py``
wraps the adapter call in::

    except Exception:
        logger.exception("Adapter raised while sending message %s", message.pk)
        return _defer(message, error=Failure.PROVIDER_UNAVAILABLE.value)

An adapter bug must not kill a flow (SPEC §9.5), which is right — and it means a
tripwire raising ``AssertionError`` would be **caught, logged and turned into a
deferred send**. The test would go green while reporting the exact bypass it was
written to catch. ``BaseException`` escapes it, and escapes the equivalent
handlers in the flow runner, the queue worker and the inbox handlers; a scan
confirms no ``except BaseException`` or bare ``except:`` exists in ``apps/``.

Belt and braces anyway: every reached call is also appended to a list, and the
assertions read the list rather than relying on the raise alone. A future broad
handler can swallow the exception; it cannot un-append the record.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from apps.channels.events import SendResult, SendStatus
from apps.channels.providers.base import Adapter
from apps.channels.registry import registered_platforms
from apps.channels.tests.fake_adapter import swapped_adapter

__all__ = ["AdapterReached", "adapter_tripwire"]


class AdapterReached(BaseException):
    """An adapter was called. Subclasses ``BaseException`` — see the module docstring."""


class _Tripwire(Adapter):
    """An adapter that cannot send, and says so loudly enough to escape."""

    platform = ""
    webhook_content = "json"
    reached: list[Any] = []

    def resolve_connection(self, request: Any, raw_body: bytes) -> Any:  # pragma: no cover - unused
        return None

    def verify_webhook(self, request: Any, connection: Any) -> bool:  # pragma: no cover - unused
        return True

    def parse_events(self, request: Any, connection: Any) -> list[Any]:  # pragma: no cover - unused
        return []

    def send(self, connection: Any, identity: Any, outbound: Any) -> SendResult:
        type(self).reached.append({"platform": connection.platform, "address": identity.platform_user_id})
        raise AdapterReached(
            f"An adapter was reached for {identity.platform_user_id} on {connection.platform}. "
            f"If that identity is opted out, SPEC §19's chokepoint has been bypassed."
        )

    def send_typing(self, connection: Any, identity: Any) -> None:  # pragma: no cover - unused
        return None

    def mark_seen(self, connection: Any, identity: Any, message_id: str) -> None:  # pragma: no cover
        return None


class _Permissive(_Tripwire):
    """The positive control: records the call and lets the send succeed.

    Without this, every "the adapter was not reached" assertion passes vacuously
    the day a driver stops driving — which for a table of send paths is the
    likeliest failure mode, not an exotic one. ``tests/idor.py`` requires at
    least one real 404 for the same reason.
    """

    def send(self, connection: Any, identity: Any, outbound: Any) -> SendResult:
        type(self).reached.append({"platform": connection.platform, "address": identity.platform_user_id})
        return SendResult(status=SendStatus.SENT, provider_message_id=f"tripwire-{len(type(self).reached)}")


@contextmanager
def adapter_tripwire(*, permissive: bool = False) -> Iterator[list[dict[str, Any]]]:
    """Replace **every** registered adapter, and yield what they were asked to send.

    Every platform, not just the one under test: a bypass that reached the wrong
    adapter would otherwise slip through, and "no adapter was reached" is the
    claim worth making.

    Installed through ``apps/channels/tests/fake_adapter.py::swapped_adapter``,
    which saves and restores the real adapter rather than unregistering it. That
    is not a nicety — ``register_adapter`` refuses a duplicate, and a private
    copy of this dance once took 31 unrelated tests down when the real Telegram
    adapter landed.
    """
    cls = _Permissive if permissive else _Tripwire
    cls.reached = []
    platforms = list(registered_platforms())

    from contextlib import ExitStack

    with ExitStack() as stack:
        for platform in platforms:
            stack.enter_context(swapped_adapter(platform, cls))
        yield cls.reached
