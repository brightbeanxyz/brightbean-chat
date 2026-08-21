"""The edge handle grammar from SPEC §9.1.

An edge's ``sourceHandle`` says *which way out* of a node it is:

    default | btn:<id> | qr:<id> | cond:true | cond:false | rand:<id> | timeout | error

The runner (L3-B) follows the edge whose handle the node returned, so a handle
this module cannot parse is a route that can never be taken. Validation rejects
it rather than letting it sit in a published graph as a silent dead end.

Which handles a *particular* node exposes depends on its type and, for the
dynamic ones, on its config — a ``btn:<id>`` is only real if a button with that
id exists. That question needs the node registry, so it lives in
:func:`apps.flows.schema.nodes.handles_for_node`; this module owns the grammar
alone and imports nothing from the registry.
"""

import re
from dataclasses import dataclass

__all__ = [
    "COND_VALUES",
    "HANDLE_PATTERN",
    "DYNAMIC_PREFIXES",
    "STATIC_HANDLES",
    "Handle",
    "format_handle",
    "parse_handle",
]

#: Handles that stand alone, with no id after them.
STATIC_HANDLES = ("default", "timeout", "error")

#: The two branches of a condition node (SPEC §11.4).
COND_VALUES = ("true", "false")

#: Prefixes whose id comes from the node's own config.
DYNAMIC_PREFIXES = ("btn", "qr", "rand")

# Ids are authored by the builder, but they arrive over the network like
# everything else, so the character set is an allowlist and the length is
# capped. ':' is excluded on purpose — it is the separator, and allowing it in
# an id would make "btn:a:b" ambiguous.
#: Exported so the generated schema constrains ``sourceHandle`` to the same
#: grammar this module parses, instead of describing it only in prose.
HANDLE_PATTERN = r"^(?:default|timeout|error|cond:(?:true|false)|(?:btn|qr|rand):[A-Za-z0-9_-]{1,64})$"

_HANDLE_RE = re.compile(HANDLE_PATTERN)


@dataclass(frozen=True)
class Handle:
    """A parsed handle: ``kind`` is the part before any colon."""

    kind: str
    value: str | None = None

    def __str__(self) -> str:
        return format_handle(self.kind, self.value)


def parse_handle(raw: object) -> Handle | None:
    """Parse a handle string, or return ``None`` when it does not fit the grammar."""
    if not isinstance(raw, str) or not _HANDLE_RE.match(raw):
        return None
    if ":" not in raw:
        return Handle(kind=raw)
    kind, value = raw.split(":", 1)
    return Handle(kind=kind, value=value)


def format_handle(kind: str, value: str | None = None) -> str:
    return kind if value is None else f"{kind}:{value}"
