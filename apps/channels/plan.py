"""The organization's channel limit, for the eight places that add a connection.

**Why this is a module and not a line.** There is no ``create_connection``
service in this app: ``connection_create`` builds its row through a form and each
of the six adapter views constructs ``ChannelConnection(...)`` itself, so there
are seven places a channel comes into existence and an eighth — re-enabling a
disabled one — that has exactly the same effect on the count. A limit enforced at
six of eight is worse than no limit, because it reads as enforced.

So the guard is one function, called at every one of them, and
``apps/channels/tests/test_plan_sites.py`` is an AST sweep asserting that the set
of modules constructing a ``ChannelConnection`` is exactly the set that calls
this. That is what stops the list growing silently when a seventh platform lands.

Extracting a real ``register_connection`` service would be the better fix and is
worth doing, but it touches six OAuth flows and belongs in its own change rather
than riding along with billing.

The return convention is ``""`` for allowed, matching the ``-> str`` shape the
adapter connect helpers already use for "no message to show".

**The six OAuth adapters count without the organization lock, and that is a
deliberate residual.** ``views.connection_create`` and
``views.connection_set_status`` take it — their critical section is a count and
a local write. The adapters' is not: each runs a platform handshake, and the
only place to hold a lock that actually covered their insert would span it.
Holding a database lock across a third-party request is how a degraded provider
becomes an exhausted connection pool, which is a worse outcome than the bug it
would close — two admins completing two different OAuth flows in the same
instant, leaving an organization one channel over its cap until one is removed.

The real fix is a ``register_connection`` service the seven construction sites
share, so the count and the insert are one function with one lock in it. That is
a refactor of six connect flows and belongs in its own change; it is what
``apps/channels/tests/test_plan_sites.py`` is watching for.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


@contextmanager
def plan_locked(workspace: Any) -> Iterator[None]:
    """Hold the organization for the length of a check-and-insert.

    The adapters call this around the block that already wraps ``save()`` —
    never around the platform handshake above it, which is a network call.
    """
    from apps.billing.entitlements import organization_locked

    with organization_locked(workspace.organization):
        yield


def plan_refusal(workspace: Any) -> str:
    """``""`` when another channel may be connected, or the message to show.

    Called after the platform handshake in the adapter views rather than before
    it, which makes the refusal late — the OAuth exchange or ``getMe`` has
    already happened. That is deliberate: placing it uniformly at the one line
    every path shares is what makes the AST sweep able to prove all eight sites
    are covered, and nothing has been persisted at that point, so a late refusal
    costs a wasted round trip and no state.

    Silent on an unlimited plan, which is every organization on a deployment
    with no billing configured.
    """
    from apps.billing.entitlements import PlanLimitError, check_can_add_channel

    try:
        check_can_add_channel(workspace.organization)
    except PlanLimitError as exc:
        return str(exc)
    return ""
