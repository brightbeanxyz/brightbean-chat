"""A small JSON-Schema interpreter — the *only* thing that validates a node config.

ROADMAP contract 2 says the schema module is "the single source of truth" for
server validation and the React config panels alike. The cheapest way for two
consumers to disagree is for each to have its own copy of the rules, so there is
only one artefact here: :mod:`apps.flows.schema.fields` builds JSON-Schema
fragments, the exported document ships those fragments verbatim, and this module
*interprets* them. Server validation and the client's schema are then the same
bytes by construction rather than by review.

It also buys the thing that made the single-source rule affordable: the
condition node embeds ``CONDITION_SCHEMA`` from ``apps.contacts.conditions``
(contract 8) as a JSON-Schema fragment. Interpreting it directly is what lets
this app validate conditions without re-declaring the operator table anywhere.

**A subset, deliberately.** Only the keywords the exported schema actually uses
are implemented, and an unrecognised keyword is ignored exactly as JSON Schema
requires. Adding a dependency for the full specification would buy nothing here
and would make the error codes generic — and the codes are the point:

* ``unknown_config_key`` — ``additionalProperties: false`` was violated. This is
  the mass-assignment guard of SECURITY-BASELINE §7 and the reason a save
  carrying one is refused rather than stored.
* ``missing_required_config`` — a ``required`` property is absent.
* ``invalid_config_value`` — everything else: wrong type, out of range, not in
  ``enum``.

``discriminator`` (the OpenAPI keyword) is honoured on ``oneOf``: with it, the
one matching variant is validated and its errors reported precisely, instead of
"does not match any of 7 alternatives". JSON Schema ignores the keyword, so the
exported document stays valid for any other consumer.

Recursion is bounded by the caller: :mod:`apps.flows.schema.envelope` applies the
depth cap to the whole document before any config is looked at, so this walk
cannot be driven deeper than that cap.
"""

import re
from typing import Any

from apps.flows.schema.issues import Issue

__all__ = [
    "CODE_INVALID_VALUE",
    "CODE_MISSING_REQUIRED",
    "CODE_UNKNOWN_KEY",
    "validate_instance",
]

CODE_UNKNOWN_KEY = "unknown_config_key"
CODE_MISSING_REQUIRED = "missing_required_config"
CODE_INVALID_VALUE = "invalid_config_value"

# Compiled once per pattern. The patterns come from this repository's own
# schemas, never from a request, so this cache cannot be grown by a caller.
_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _pattern(expression: str) -> re.Pattern[str]:
    compiled = _PATTERN_CACHE.get(expression)
    if compiled is None:
        compiled = re.compile(expression)
        _PATTERN_CACHE[expression] = compiled
    return compiled


def _type_matches(expected: str, value: Any) -> bool:
    """JSON type test.

    ``isinstance(True, int)`` is True in Python, so booleans have to be excluded
    from the numeric types explicitly — otherwise ``{"timeout_s": true}`` passes
    an ``integer`` check and reaches the engine as a duration.
    """
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow a local ``$ref``. Only ``#/$defs/<name>`` is supported."""
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    name = ref.rsplit("/", 1)[-1]
    target = defs.get(name)
    if not isinstance(target, dict):
        # A dangling $ref is a bug in this repository's schemas, not in user
        # input, and the schema-export test catches it. Treat it as "no
        # constraints" rather than raising in a request path.
        return {}
    return target


def validate_instance(
    schema: dict[str, Any],
    value: Any,
    *,
    path: str,
    node_id: str | None = None,
    defs: dict[str, Any] | None = None,
) -> list[Issue]:
    """Validate ``value`` against ``schema``, returning findings (never raising).

    ``path`` is the dotted address reported back to the builder
    (``config.buttons[0].label``); ``defs`` is the document's ``$defs`` map, used
    to resolve local ``$ref``s.
    """
    defs = defs if defs is not None else {}
    schema = _resolve(schema, defs)
    issues: list[Issue] = []

    def add(code: str, message: str, at: str) -> None:
        issues.append(Issue(code=code, message=message, stage="document", node_id=node_id, path=at))

    types = schema.get("type")
    if types is not None:
        expected = types if isinstance(types, list) else [types]
        if not any(_type_matches(str(t), value) for t in expected):
            add(CODE_INVALID_VALUE, f"Expected {' or '.join(str(t) for t in expected)}.", path)
            # Every further keyword assumes the type held; stop here so one
            # wrong type does not produce a cascade of unrelated complaints.
            return issues

    if "const" in schema and value != schema["const"]:
        add(CODE_INVALID_VALUE, f"Must be {schema['const']!r}.", path)

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        add(CODE_INVALID_VALUE, f"Must be one of: {', '.join(repr(option) for option in enum)}.", path)

    if isinstance(value, str):
        issues.extend(_check_string(schema, value, path, node_id))
    elif isinstance(value, int | float) and not isinstance(value, bool):
        issues.extend(_check_number(schema, value, path, node_id))
    elif isinstance(value, list):
        issues.extend(_check_array(schema, value, path, node_id, defs))
    elif isinstance(value, dict):
        issues.extend(_check_object(schema, value, path, node_id, defs))

    if "oneOf" in schema or "anyOf" in schema:
        issues.extend(_check_variants(schema, value, path, node_id, defs))

    return issues


def _issue(code: str, message: str, path: str, node_id: str | None) -> Issue:
    return Issue(code=code, message=message, stage="document", node_id=node_id, path=path)


def _check_string(schema: dict[str, Any], value: str, path: str, node_id: str | None) -> list[Issue]:
    issues: list[Issue] = []
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Must be at least {minimum} character(s).", path, node_id))
    if isinstance(maximum, int) and len(value) > maximum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Must be at most {maximum} character(s).", path, node_id))
    expression = schema.get("pattern")
    if isinstance(expression, str) and not _pattern(expression).search(value):
        issues.append(_issue(CODE_INVALID_VALUE, "Does not match the required format.", path, node_id))
    return issues


def _check_number(schema: dict[str, Any], value: float, path: str, node_id: str | None) -> list[Issue]:
    issues: list[Issue] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(minimum, int | float) and value < minimum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Must be at least {minimum}.", path, node_id))
    if isinstance(maximum, int | float) and value > maximum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Must be at most {maximum}.", path, node_id))
    return issues


def _check_array(
    schema: dict[str, Any], value: list[Any], path: str, node_id: str | None, defs: dict[str, Any]
) -> list[Issue]:
    issues: list[Issue] = []
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if isinstance(minimum, int) and len(value) < minimum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Needs at least {minimum} item(s).", path, node_id))
    if isinstance(maximum, int) and len(value) > maximum:
        issues.append(_issue(CODE_INVALID_VALUE, f"Takes at most {maximum} item(s).", path, node_id))
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            issues.extend(validate_instance(items, item, path=f"{path}[{index}]", node_id=node_id, defs=defs))
    return issues


def _check_object(
    schema: dict[str, Any], value: dict[str, Any], path: str, node_id: str | None, defs: dict[str, Any]
) -> list[Issue]:
    issues: list[Issue] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}

    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if name not in value:
                issues.append(
                    _issue(
                        CODE_MISSING_REQUIRED,
                        f"{name!r} is required.",
                        f"{path}.{name}" if path else str(name),
                        node_id,
                    )
                )

    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in properties:
                issues.append(
                    _issue(
                        CODE_UNKNOWN_KEY,
                        f"{name!r} is not a recognised key here. Allowed: {', '.join(sorted(properties)) or 'none'}.",
                        f"{path}.{name}" if path else str(name),
                        node_id,
                    )
                )

    for name, sub_schema in properties.items():
        if name in value and isinstance(sub_schema, dict):
            issues.extend(
                validate_instance(
                    sub_schema, value[name], path=f"{path}.{name}" if path else str(name), node_id=node_id, defs=defs
                )
            )
    return issues


def _check_variants(
    schema: dict[str, Any], value: Any, path: str, node_id: str | None, defs: dict[str, Any]
) -> list[Issue]:
    """``oneOf``/``anyOf``, with the OpenAPI ``discriminator`` honoured."""
    variants = schema.get("oneOf") or schema.get("anyOf") or []
    variants = [v for v in variants if isinstance(v, dict)]
    if not variants:
        return []

    discriminator = schema.get("discriminator")
    if isinstance(discriminator, dict) and isinstance(value, dict):
        selected = _select_variant(discriminator, value, path, node_id, defs)
        if isinstance(selected, list):
            return selected
        return validate_instance(selected, value, path=path, node_id=node_id, defs=defs)

    # No discriminator: report the near-miss rather than every alternative's
    # complaints. Fewest findings is the closest match by any useful measure,
    # and it keeps the panel's error list readable.
    attempts = [validate_instance(variant, value, path=path, node_id=node_id, defs=defs) for variant in variants]
    if any(not attempt for attempt in attempts):
        return []
    return min(attempts, key=len)


def _select_variant(
    discriminator: dict[str, Any], value: dict[str, Any], path: str, node_id: str | None, defs: dict[str, Any]
) -> dict[str, Any] | list[Issue]:
    """The variant named by the discriminator property, or the findings that stop us."""
    prop = discriminator.get("propertyName")
    mapping = discriminator.get("mapping")
    if not isinstance(prop, str) or not isinstance(mapping, dict):
        return {}
    at = f"{path}.{prop}" if path else prop
    if prop not in value:
        return [_issue(CODE_MISSING_REQUIRED, f"{prop!r} is required.", at, node_id)]
    key = value[prop]
    if not isinstance(key, str) or key not in mapping:
        allowed = ", ".join(repr(option) for option in sorted(mapping))
        return [_issue(CODE_INVALID_VALUE, f"Must be one of: {allowed}.", at, node_id)]
    return _resolve({"$ref": mapping[key]}, defs)
