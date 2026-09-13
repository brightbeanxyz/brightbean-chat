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
"""

from typing import Any


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
