"""What a brand-new flow opens with.

A blank grid is technically correct and practically useless: the author has to
know to click a palette item, then know to drag from the ``Next`` dot to wire
the second one, then discover that Triggers is a separate button elsewhere.
Every one of those is learnable and none is guessable, and the first thing the
canvas said was an error about a node they had not added yet.

**Not in ``fixtures.py``.** That module's ``send_message`` is deliberately
maximal — images, two buttons, a quick reply, a followup, a retry — because it
exists to give the test suite one of everything. A first step should be the
smallest thing that is worth looking at.

**Not in ``schema/envelope.py``.** That module is about the envelope and knows
no node types; importing the node registry into it would invert the dependency.

**The copy is a real sentence, not a placeholder.** ``blocks`` requires at least
one item and a text block requires at least one character, so a seed built from
empty strings would greet the author with a red banner about a node they never
touched — the same reasoning ``frontend/builder/src/schema/sample.ts`` gives for
generating valid config rather than blank config. Changing the node type here
means re-checking that the result still validates clean on every platform, which
``tests/test_starter.py`` does.
"""

from typing import Any

from apps.flows.schema.envelope import SCHEMA_VERSION

__all__ = ["starter_graph"]

#: Far enough in to leave room for the trigger cards, which sit to the *left* of
#: whichever node starts the flow (frontend/builder/src/canvas/triggerNodes.ts).
#: At x=160 the card landed at x=-160 and opened outside the visible pane, so
#: the one thing a brand-new flow most needs to show was the one thing off
#: screen.
_POSITION = {"x": 520, "y": 160}

_FIRST_MESSAGE = "Hi! Thanks for getting in touch — how can we help?"


def starter_graph() -> dict[str, Any]:
    """One ``send_message`` node, wired to nothing, valid and publishable."""
    return {
        "schema": SCHEMA_VERSION,
        "nodes": [
            {
                "id": "n1",
                "type": "send_message",
                "position": dict(_POSITION),
                "config": {"blocks": [{"type": "text", "text": _FIRST_MESSAGE}]},
            }
        ],
        "edges": [],
    }
