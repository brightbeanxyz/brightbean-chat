"""The authoring vocabulary for node configs — small helpers that build JSON Schema.

These return plain JSON-Schema fragments rather than a parallel object model,
because the fragment *is* the artefact: :mod:`apps.flows.schema.export` ships it
to the React builder and :mod:`apps.flows.schema.jsonschema` interprets the same
bytes on the server. One representation, so the two consumers ROADMAP contract 2
names cannot drift.

:func:`obj` sets ``additionalProperties: false`` on every object it builds, and
there is no opt-out. Unknown-key rejection is the mass-assignment guard of
SECURITY-BASELINE §7, and a guard you have to remember to switch on is one that
will eventually be forgotten on the object that mattered.
"""

from collections.abc import Sequence
from typing import Any

__all__ = [
    "any_json",
    "const",
    "array",
    "boolean",
    "enum",
    "integer",
    "obj",
    "ref",
    "string",
    "tagged_union",
]


def _described(schema: dict[str, Any], description: str) -> dict[str, Any]:
    if description:
        schema["description"] = description
    return schema


def string(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    """A string. ``min_length=1`` is the usual way to say "not blank"."""
    schema: dict[str, Any] = {"type": "string"}
    if min_length is not None:
        schema["minLength"] = min_length
    if max_length is not None:
        schema["maxLength"] = max_length
    if pattern is not None:
        schema["pattern"] = pattern
    return _described(schema, description)


def integer(*, minimum: int | None = None, maximum: int | None = None, description: str = "") -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return _described(schema, description)


def boolean(*, description: str = "") -> dict[str, Any]:
    return _described({"type": "boolean"}, description)


def enum(*values: str, description: str = "") -> dict[str, Any]:
    return _described({"type": "string", "enum": list(values)}, description)


def const(value: str, *, description: str = "") -> dict[str, Any]:
    """A string fixed to one value — how a tagged union's variants pin their tag."""
    return _described({"type": "string", "const": value}, description)


def array(
    items: dict[str, Any],
    *,
    min_items: int | None = None,
    max_items: int | None = None,
    description: str = "",
) -> dict[str, Any]:
    """A list. ``max_items`` is not decoration — it is a per-field input limit."""
    schema: dict[str, Any] = {"type": "array", "items": items}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    return _described(schema, description)


def obj(
    properties: dict[str, dict[str, Any]],
    *,
    required: Sequence[str] = (),
    description: str = "",
) -> dict[str, Any]:
    """An object that accepts exactly ``properties`` and nothing else."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return _described(schema, description)


def ref(name: str) -> dict[str, Any]:
    """A reference to ``#/$defs/<name>`` in the exported document."""
    return {"$ref": f"#/$defs/{name}"}


def tagged_union(discriminator: str, variants: dict[str, str], *, description: str = "") -> dict[str, Any]:
    """One of several object shapes, chosen by a string property.

    ``variants`` maps the discriminator's value to a ``$defs`` name. The
    ``discriminator`` keyword is OpenAPI's, not JSON Schema's — every JSON-Schema
    consumer ignores it, while ours uses it to validate the *one* variant the
    payload asked for and report its errors precisely, rather than emitting
    "matches none of seven alternatives" and leaving the panel nothing to
    highlight.
    """
    # dict.fromkeys, not set(): several discriminator values may point at one
    # variant (the four media block types share a shape), and the branch list
    # has to stay both deduplicated and deterministically ordered.
    schema: dict[str, Any] = {
        "type": "object",
        "oneOf": [ref(name) for name in dict.fromkeys(variants.values())],
        "discriminator": {
            "propertyName": discriminator,
            "mapping": {value: f"#/$defs/{name}" for value, name in variants.items()},
        },
    }
    return _described(schema, description)


def any_json(*, description: str = "") -> dict[str, Any]:
    """Any JSON value.

    Used only where the *shape* genuinely belongs to the user — the External
    Request body template is the one case. Unconstrained here does not mean
    unbounded: the document-level size and depth caps in
    :mod:`apps.flows.schema.envelope` are applied before any config is read.
    """
    return _described({}, description)
