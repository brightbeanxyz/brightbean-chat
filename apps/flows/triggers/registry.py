"""One row per trigger type: what it is called, where it can fire, what it configures.

SPEC §10 is a table, so this is a table. Writing it as data rather than as
branches is what lets four unrelated consumers agree without talking to each
other:

* :mod:`apps.flows.triggers.matching` gates an unbound trigger on ``platforms``;
* :func:`apps.flows.triggers.platforms.platforms_for_flow` recomputes the §16
  capability warnings from the same column;
* the connection picker in the panel offers exactly ``platforms``;
* :mod:`apps.flows.triggers.forms` refuses a binding the picker did not offer,
  because the client list is a convenience and never a gate.

Registration is open. L5-A's story and follow triggers and L6-A's rule binding
add a **matcher**, not a spec — every spec ships here today, so a type whose
runtime lands later still validates, still renders in the panel, and still
counts towards a flow's platform set.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from apps.flows.triggers import schema as trigger_schema
from apps.flows.triggers.types import PLATFORMS_FOR_TYPE, TriggerType

__all__ = [
    "TRIGGER_TYPES",
    "TriggerSpec",
    "bindable_types",
    "register_trigger_type",
    "spec_for",
]


@dataclass(frozen=True)
class TriggerSpec:
    """Everything about a trigger type that is not behaviour."""

    type: str
    label: str
    description: str
    #: SPEC §10's "Channels" column. Empty means the type is not delivered by a
    #: platform at all, which is a different thing from "every platform".
    platforms: frozenset[str]
    #: JSON Schema for ``Trigger.config_json``. Built with
    #: :mod:`apps.flows.schema.fields`, so ``additionalProperties: false`` is
    #: there without anyone remembering it (SECURITY-BASELINE §7).
    config: dict[str, Any] = field(default_factory=dict)
    #: A fresh, valid, empty config — what "add a trigger" starts from.
    default_config: Callable[[], dict[str, Any]] = dict
    #: May a trigger of this type name a single connection? False for the two
    #: types no channel delivers.
    bindable: bool = True
    #: Runs as its own routing stage rather than competing in the match
    #: (``default_reply``, SPEC §9.3 step 4).
    stage_only: bool = False
    #: Fired only through an entry point — SPEC §10's ``api``.
    entrypoint_only: bool = False


TRIGGER_TYPES: dict[str, TriggerSpec] = {}


def register_trigger_type(spec: TriggerSpec, *, replace: bool = False) -> TriggerSpec:
    """Add ``spec`` to the registry. A duplicate raises unless ``replace``."""
    if spec.type in TRIGGER_TYPES and not replace:
        raise ValueError(f"{spec.type!r} is already a registered trigger type.")
    TRIGGER_TYPES[spec.type] = spec
    return spec


def spec_for(trigger_type: str) -> TriggerSpec | None:
    """The spec for a type, or ``None`` for one nothing registered."""
    return TRIGGER_TYPES.get(trigger_type)


def bindable_types() -> tuple[str, ...]:
    """Types a trigger may attach to one channel connection."""
    return tuple(sorted(spec.type for spec in TRIGGER_TYPES.values() if spec.bindable))


def _spec(
    trigger_type: str,
    label: str,
    description: str,
    config: dict[str, Any],
    **extra: Any,
) -> TriggerSpec:
    return TriggerSpec(
        type=trigger_type,
        label=label,
        description=description,
        platforms=PLATFORMS_FOR_TYPE[trigger_type],
        config=config,
        **extra,
    )


register_trigger_type(
    _spec(
        TriggerType.KEYWORD,
        "Keyword",
        "Runs when an incoming message matches one of these words.",
        trigger_schema.KEYWORD,
        default_config=lambda: {"keywords": []},
    )
)
register_trigger_type(
    _spec(
        TriggerType.COMMENT,
        "Comment",
        "Runs when someone comments on a post, and replies to them privately.",
        trigger_schema.COMMENT,
        default_config=lambda: {
            "post_scope": "all",
            "post_ids": [],
            "include_keywords": [],
            "exclude_keywords": [],
            "top_level_only": True,
            "public_reply": {"mode": "none", "texts": []},
            "like_comment": False,
            "once_per_contact_per_post": True,
        },
    )
)
register_trigger_type(
    _spec(
        TriggerType.STORY_MENTION,
        "Story mention",
        "Runs when someone mentions this account in their story.",
        trigger_schema.STORY_MENTION,
    )
)
register_trigger_type(
    _spec(
        TriggerType.STORY_REPLY,
        "Story reply",
        "Runs when someone replies to one of this account's stories.",
        trigger_schema.STORY_REPLY,
        default_config=lambda: {"keywords": []},
    )
)
register_trigger_type(
    _spec(
        TriggerType.FOLLOW,
        "New follower",
        "Runs when someone follows this account.",
        trigger_schema.FOLLOW,
    )
)
register_trigger_type(
    _spec(
        TriggerType.REF_URL,
        "Ref URL / QR",
        "Runs when someone arrives through a link or QR code carrying this reference.",
        trigger_schema.REF_URL,
        default_config=lambda: {"ref": ""},
    )
)
register_trigger_type(
    _spec(
        TriggerType.DEFAULT_REPLY,
        "Default reply",
        "Runs when nothing else matched. At most once per contact per day.",
        trigger_schema.DEFAULT_REPLY,
        stage_only=True,
    )
)
register_trigger_type(
    _spec(
        TriggerType.WELCOME,
        "Welcome",
        "Runs the first time someone opens a conversation.",
        trigger_schema.WELCOME,
    )
)
register_trigger_type(
    _spec(
        TriggerType.RULE,
        "Rule",
        "Runs when something happens to a contact — a tag added, a field changed.",
        trigger_schema.RULE,
        bindable=False,
    )
)
register_trigger_type(
    _spec(
        TriggerType.API,
        "API",
        "Runs only when the API asks for it.",
        trigger_schema.API,
        bindable=False,
        entrypoint_only=True,
    )
)
