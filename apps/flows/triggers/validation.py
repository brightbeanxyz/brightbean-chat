"""Validate a trigger's ``config_json`` — caps first, then the schema.

The order is copied from :func:`apps.flows.schema.envelope.check_limits`, and it
is the order for a reason: measuring depth by walking a document you have not
bounded is how a deeply nested payload becomes a recursion error instead of a
validation finding. So size, then depth, then the schema walk — and each of the
first two returns immediately, because a document that failed them cannot be
meaningfully described by the findings the third would produce.

Never raises. A finding refuses the write and is rendered in the panel; an
exception would be a 500 on a form somebody typed into.
"""

from typing import Any

from django.utils.translation import gettext

from apps.common.jsonsize import jsonb_byte_size
from apps.flows.schema.envelope import json_depth
from apps.flows.schema.issues import Issue
from apps.flows.schema.jsonschema import CODE_INVALID_VALUE, validate_instance
from apps.flows.schema.nodes import all_defs
from apps.flows.triggers.registry import spec_for

__all__ = [
    "MAX_TRIGGER_CONFIG_BYTES",
    "MAX_TRIGGER_CONFIG_DEPTH",
    "config_byte_size",
    "validate_config",
]

#: A trigger config is a keyword list, not a graph. Three orders of magnitude
#: below ``MAX_GRAPH_BYTES`` because there is nothing here that legitimately
#: grows: the widest shape is ``MAX_KEYWORDS`` keywords of ``MAX_KEYWORD_CHARS``.
MAX_TRIGGER_CONFIG_BYTES = 16 * 1024

#: The deepest legitimate path is ``config → public_reply → texts → item`` at
#: four. Eight leaves room for the rule trigger's nested condition filter.
MAX_TRIGGER_CONFIG_DEPTH = 8


def config_byte_size(config: Any) -> int | None:
    """Bytes this config occupies as jsonb, or ``None`` when it cannot be stored.

    Both answers come from :func:`apps.common.jsonsize.jsonb_byte_size`, which
    is also what :func:`apps.flows.schema.envelope.graph_byte_size` measures
    with, so the two caps can no longer disagree about what a byte is.

    This used to answer ``MAX_TRIGGER_CONFIG_BYTES + 1`` for a config that
    would not serialize, which routed an unstorable value to the size refusal
    and told the author their configuration was 16385 bytes. It is not: a lone
    surrogate in one reply makes a three-hundred-byte config unstorable.
    :func:`validate_config` names that case separately now.
    """
    return jsonb_byte_size(config)


def validate_config(trigger_type: str, config: Any, *, known_size: int | None = None) -> list[Issue]:
    """Findings for ``config`` against ``trigger_type``'s schema. Empty means valid.

    ``known_size`` is an **upper bound** a caller already has — the request
    body a config was parsed out of cannot be smaller than the config — and
    supplying it skips a re-serialisation. It is a bound, not an answer: one
    that is itself over the cap falls through to measuring precisely, exactly
    as ``check_limits`` does, so a config is never refused for the size of the
    envelope it arrived in and the message always names the real size.
    """
    spec = spec_for(trigger_type)
    if spec is None:
        return [_issue(gettext("%(type)r is not a trigger type.") % {"type": trigger_type}, path="type")]

    if not isinstance(config, dict):
        return [_issue(gettext("A trigger's configuration has to be an object."), path="config")]

    if known_size is None or known_size > MAX_TRIGGER_CONFIG_BYTES:
        size = config_byte_size(config)
        if size is None:
            # Not a size problem, so it must not be reported as one: NaN, a
            # lone surrogate or an unserializable type is a value jsonb will
            # refuse at any length.
            return [
                _issue(
                    gettext("The configuration holds a value that cannot be stored as JSON."),
                    path="config",
                )
            ]
        if size > MAX_TRIGGER_CONFIG_BYTES:
            return [
                _issue(
                    gettext("The configuration is %(size)s bytes; the limit is %(max)s.")
                    % {"size": size, "max": MAX_TRIGGER_CONFIG_BYTES},
                    path="config",
                )
            ]

    if json_depth(config, limit=MAX_TRIGGER_CONFIG_DEPTH) > MAX_TRIGGER_CONFIG_DEPTH:
        return [
            _issue(
                gettext("The configuration nests deeper than %(max)s levels.") % {"max": MAX_TRIGGER_CONFIG_DEPTH},
                path="config",
            )
        ]

    return validate_instance(spec.config, config, path="config", defs=all_defs())


def _issue(message: str, *, path: str) -> Issue:
    return Issue(code=CODE_INVALID_VALUE, message=message, stage="document", node_id=None, path=path)
