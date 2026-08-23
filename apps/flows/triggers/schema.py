"""JSON Schema for each trigger type's ``config_json`` (SPEC §10).

Built entirely from :mod:`apps.flows.schema.fields`, which is the point rather
than a convenience: ``obj()`` emits ``additionalProperties: false`` with no way
to opt out, so SECURITY-BASELINE §7's "reject unknown keys" mass-assignment
guard is structural here instead of remembered. The same builders, the same
``$defs``, and the same interpreter (:mod:`apps.flows.schema.jsonschema`) the
graph already uses, so a trigger config reports the same three issue codes a
node config does and the panel needs no second error vocabulary.

**These schemas are deliberately not exported to** ``static/flows/flow-schema.json``.
That artefact describes a *graph*, it is byte-compared by a test, and nothing
generates trigger forms from a schema — the panel is Django and HTMX, and the
builder gets a rendered read-only summary. :func:`trigger_json_schema` exists so
#25 can serve it over the public API later without this module changing shape.
"""

from typing import Any

from apps.flows.schema import fields as f
from apps.flows.triggers.types import MAX_REF_CHARS, REF_PATTERN

__all__ = [
    "API",
    "COMMENT",
    "DEFAULT_REPLY",
    "FOLLOW",
    "KEYWORD",
    "MAX_KEYWORDS",
    "MAX_KEYWORD_CHARS",
    "REF_URL",
    "RULE",
    "STORY_MENTION",
    "STORY_REPLY",
    "WELCOME",
    "trigger_json_schema",
]

#: Bounds on the one list a user can grow without limit. A hundred keywords is
#: far past any real configuration and still cheap to scan once per event.
MAX_KEYWORDS = 100
MAX_KEYWORD_CHARS = 200
MAX_POST_IDS = 50
MAX_PUBLIC_REPLIES = 20
MAX_PUBLIC_REPLY_CHARS = 500

#: SPEC §10: "mode per keyword", so the mode belongs to the keyword rather than
#: to the trigger. One trigger can match "help" exactly and "refund" anywhere.
_KEYWORD = f.obj(
    {
        "text": f.string(min_length=1, max_length=MAX_KEYWORD_CHARS, description="The word or phrase to look for."),
        "mode": f.enum(
            "exact",
            "contains",
            "any_word",
            description="exact: the whole message. contains: anywhere, even inside a word. any_word: a whole word.",
        ),
    },
    required=["text", "mode"],
)

KEYWORD = f.obj(
    {"keywords": f.array(_KEYWORD, min_items=1, max_items=MAX_KEYWORDS)},
    required=["keywords"],
    description="SPEC §10 keyword trigger.",
)

REF_URL = f.obj(
    {
        "ref": f.string(
            min_length=1,
            max_length=MAX_REF_CHARS,
            pattern=REF_PATTERN,
            description="Letters, digits, underscore and hyphen. Appears in the link and the QR code.",
        ),
        "link_handle": f.string(
            max_length=100,
            description=(
                "The account's public handle, when the platform needs one to build a deep link "
                "(a Telegram bot's username, say). Blank uses whatever the connection reported."
            ),
        ),
    },
    required=["ref"],
    description="SPEC §10 ref-URL trigger.",
)

#: SPEC §10: "optional keywords". No keywords means every story reply matches.
STORY_REPLY = f.obj(
    {"keywords": f.array(_KEYWORD, max_items=MAX_KEYWORDS)},
    description="SPEC §10 story-reply trigger.",
)

COMMENT = f.obj(
    {
        "post_scope": f.enum("all", "specific", description="Every post, or only the listed ones."),
        "post_ids": f.array(f.string(min_length=1, max_length=200), max_items=MAX_POST_IDS),
        "include_keywords": f.array(f.string(min_length=1, max_length=MAX_KEYWORD_CHARS), max_items=MAX_KEYWORDS),
        "exclude_keywords": f.array(f.string(min_length=1, max_length=MAX_KEYWORD_CHARS), max_items=MAX_KEYWORDS),
        "top_level_only": f.boolean(description="Ignore replies to other comments."),
        "public_reply": f.obj(
            {
                "mode": f.enum("none", "static", "random"),
                "texts": f.array(
                    f.string(min_length=1, max_length=MAX_PUBLIC_REPLY_CHARS), max_items=MAX_PUBLIC_REPLIES
                ),
            },
            required=["mode"],
        ),
        "like_comment": f.boolean(),
        "once_per_contact_per_post": f.boolean(),
    },
    required=["post_scope"],
    description="SPEC §10 comment trigger. L5-A and L5-B add the platform matcher and the reply/like calls.",
)

#: SPEC §10 says "none" for all four of these. An empty object rather than
#: ``any_json()``: "no configuration" and "any configuration" are opposites, and
#: only one of them refuses a key somebody typed by mistake.
WELCOME = f.obj({}, description="SPEC §10 welcome trigger. No configuration.")
STORY_MENTION = f.obj({}, description="SPEC §10 story-mention trigger. No configuration.")
FOLLOW = f.obj({}, description="SPEC §10 follow trigger. No configuration.")
DEFAULT_REPLY = f.obj({}, description="SPEC §10 default reply. The 24-hour guard is fixed, not configured.")
API = f.obj(
    {
        "key": f.string(
            max_length=100,
            description="Optional name the API names this trigger by, when a flow has more than one.",
        )
    },
    description="SPEC §10 API trigger. Fired only by the public flow-start endpoint (#25).",
)

#: The id shape both event filters take. Same pattern the condition engine uses
#: for a rule ``key``; a string rather than a format, because that is the one
#: keyword every consumer of these schemas already implements.
_UUID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

#: SPEC §10's rule trigger. ``filters`` references the condition schema the
#: condition node already registered into ``$defs`` (ROADMAP contract 8).
#:
#: ``tag_id`` and ``field_id`` are L6-A's addition and they are not the same
#: thing as ``filters``. A condition document describes the *contact*, so the
#: closest it can say is "somebody who now has the VIP tag" — which fires when
#: any tag at all is added to a VIP. SPEC §10's "optional filters (tag id …)"
#: means the tag that was **just added**, and that id lives in the event payload
#: rather than on the contact, so it needs its own key. Both are optional and
#: each is only consulted for the events that carry the matching id
#: (``apps/campaigns/rules.py``).
RULE = f.obj(
    {
        "event": f.enum(
            "tag_added",
            "tag_removed",
            "field_changed",
            "sequence_subscribed",
            "sequence_unsubscribed",
            "contact_created",
        ),
        "tag_id": f.string(
            pattern=_UUID_PATTERN,
            description="Only fire for this tag. Blank fires for any tag. tag_added / tag_removed only.",
        ),
        "field_id": f.string(
            pattern=_UUID_PATTERN,
            description="Only fire for this custom field. Blank fires for any field. field_changed only.",
        ),
        "filters": f.ref("condition_filter"),
    },
    required=["event"],
    description="SPEC §10 rule trigger. L6-A binds it to the internal event catalogue.",
)


def trigger_json_schema() -> dict[str, Any]:
    """Every trigger type's config schema, in one document.

    Not served anywhere yet. It exists so #25 can publish it without this module
    growing an HTTP concern, and so a test can assert every registered type has
    a schema without importing the registry (which imports this module).
    """
    from apps.flows.schema.nodes import all_defs
    from apps.flows.triggers.registry import TRIGGER_TYPES

    return {
        "$defs": all_defs(),
        "triggerTypes": {spec.type: spec.config for spec in TRIGGER_TYPES.values()},
    }
