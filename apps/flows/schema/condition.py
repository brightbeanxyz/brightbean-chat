"""The condition node's config schema — ROADMAP contract 8's consumer side.

``apps.contacts.conditions`` (issue #3) owns ``CONDITION_SCHEMA``, ``evaluate()``
and ``queryset()``. The condition node embeds that schema; it does **not**
re-declare the operator table, because two copies of an operator table is two
answers to "does ``contains`` apply to a number field", and the engine and the
builder would eventually pick different ones.

#3 is a parallel sibling that has not merged yet, so this module is the single
swap point: the import wins the moment the contacts app exists, and the
fallback below is SPEC §11.4's written form, which is what the issue instructs
this workstream to code against in the meantime. Nothing else in this app
mentions an operator — everything reads :data:`CONDITION_SCHEMA` from here — so
the swap is this file and no other.

When #3 lands, ``apps/flows/tests/test_condition_contract.py`` starts asserting
that the imported schema is the one in force, and the fallback becomes dead
weight that can be deleted in a one-line follow-up.
"""

from typing import Any

__all__ = ["CONDITION_SCHEMA", "CONDITION_SCHEMA_IS_VENDORED"]

# SPEC §11.4, written out. Deliberately self-contained: it carries no ``$defs``
# and no ``$ref``, so it drops into the exported document at any position
# without its internal references needing to be rewritten.
_SPEC_11_4_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Condition filter",
    "description": (
        "SPEC §11.4. Vendored form, in force only until apps.contacts.conditions "
        "(issue #3) is importable; see apps/flows/schema/condition.py."
    ),
    "additionalProperties": False,
    "required": ["match", "rules"],
    "properties": {
        "match": {
            "type": "string",
            "enum": ["all", "any"],
            "description": "Whether every rule must hold, or any one of them.",
        },
        "rules": {
            "type": "array",
            # A generous ceiling that still bounds the ORM query #3 compiles
            # this into (SECURITY-BASELINE §7).
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "op"],
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["tag", "custom_field", "system_field", "segment", "sequence", "window"],
                    },
                    "key": {
                        "type": "string",
                        "maxLength": 200,
                        "description": (
                            "What the rule is about: a tag name, a custom-field name, a system field, "
                            "a segment id. Omitted for sources that need no key, such as window."
                        ),
                    },
                    "op": {
                        "type": "string",
                        "description": (
                            "Operators by value type — text: is, is_not, contains, has_value, no_value; "
                            "number: =, !=, >, <, >=, <=; date/datetime: before, after, on, days_ago, "
                            "days_from_now; boolean: is; tag: has, has_not; sequence: subscribed, "
                            "not_subscribed; window: inside, outside."
                        ),
                        "enum": [
                            "is",
                            "is_not",
                            "contains",
                            "has_value",
                            "no_value",
                            "=",
                            "!=",
                            ">",
                            "<",
                            ">=",
                            "<=",
                            "before",
                            "after",
                            "on",
                            "days_ago",
                            "days_from_now",
                            "has",
                            "has_not",
                            "subscribed",
                            "not_subscribed",
                            "inside",
                            "outside",
                        ],
                    },
                    "value": {
                        "type": ["string", "number", "boolean", "null"],
                        "description": "Absent for the operators that take no operand (has_value, no_value, has).",
                    },
                },
            },
        },
    },
}

_imported: dict[str, Any] | None
try:  # pragma: no cover - the branch taken depends on whether #3 has merged
    from apps.contacts.conditions import CONDITION_SCHEMA as _CONTACTS_SCHEMA

    _imported = _CONTACTS_SCHEMA
except ImportError:
    _imported = None

#: True while the fallback above is in force. Tests assert on it so that "#3
#: merged but the import silently did not take" is a red build rather than a
#: surprise in Layer 3.
CONDITION_SCHEMA_IS_VENDORED = _imported is None

CONDITION_SCHEMA: dict[str, Any] = _imported if _imported is not None else _SPEC_11_4_SCHEMA
