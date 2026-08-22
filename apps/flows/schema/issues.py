"""The one shape a validation finding takes, and the vocabulary of codes.

Every finding is machine-readable — the React builder (L3-C) pins a message to
the node or edge it came from, so a free-text list would force it to parse
prose. ``code`` is the stable identifier; ``message`` is for humans and may be
reworded without breaking a client.

Findings are split three ways, and the split is load bearing:

``document`` errors
    The payload is not something this server will persist: it is too big, too
    deep, malformed, or carries a key no node type declares (the
    mass-assignment guard, SECURITY-BASELINE §7). A save carrying one of these
    is refused outright and **nothing is written**.
``graph`` errors
    The graph is well-formed but not runnable — a dangling edge, no entry node,
    a handle the source node does not expose. These are saved happily: a draft
    is *allowed* to be half-wired, and an autosaving canvas that refused them
    would throw away the user's work mid-edit. They block **publish**.
``warnings``
    Never block anything. Channel-capability mismatches and unreachable nodes.

SPEC §9.1 asks for validation "on save and publish"; SPEC §16 has the save
response carry the findings. Both are true here — the difference is only which
tier stops the write.
"""

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["Issue", "Stage"]

Stage = Literal["document", "graph"]


@dataclass(frozen=True)
class Issue:
    """One validation finding, addressed at the thing that caused it."""

    code: str
    message: str
    stage: Stage = "graph"
    node_id: str | None = None
    edge_id: str | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The JSON form the builder consumes. Empty addresses are omitted."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        for key, value in (("node_id", self.node_id), ("edge_id", self.edge_id), ("path", self.path)):
            if value is not None:
                payload[key] = value
        return payload
