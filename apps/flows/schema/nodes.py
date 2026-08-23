"""The node-type registry — every node in SPEC §11, config schema included.

ROADMAP contract 2: this module is the single source of truth that L3-B (the
engine) and L3-C (the React builder) both build on, so **every** node type ships
its schema now, including the ones whose runtime lands in a later layer
(``external_request`` is L4-E, ``send_sms``/``send_email`` are L5-D/E). A node
type whose schema arrives with its runtime would mean the builder cannot draw it
and the validator cannot check it until then.

The registry is data. :func:`register_node_type` is how a later issue appends
one, and :func:`register_action_verb` is contract 5's action-verb registry —
both additive, neither requiring an edit to anything already here.

Two properties are worth stating because they are what the rest of the app
leans on:

* **Every object is closed.** :func:`apps.flows.schema.fields.obj` always emits
  ``additionalProperties: false``, so an unknown config key is rejected wherever
  it appears, at any nesting depth (SECURITY-BASELINE §7's mass-assignment
  guard).
* **Handles are derived, never listed twice.** A ``btn:<id>`` handle exists
  exactly when a button with that id is in the config, so
  :func:`handles_for_node` reads the config rather than trusting a second list
  that could disagree with it.
"""

from dataclasses import dataclass, field
from typing import Any

from apps.flows.schema import fields as f
from apps.flows.schema.condition import CONDITION_SCHEMA

__all__ = [
    "ACTION_VERBS",
    "GROUPS",
    "NODE_TYPES",
    "SHARED_DEFS",
    "NodeSpec",
    "all_defs",
    "handles_for_node",
    "node_spec",
    "register_action_verb",
    "register_defs",
    "register_node_type",
]


@dataclass(frozen=True)
class NodeSpec:
    """One node type: what its config may contain and where its edges may leave."""

    type: str
    label: str
    description: str
    config: dict[str, Any]

    #: Which palette drawer the builder files this node under (issue #10). It is
    #: a property of the node type, not of the canvas: whoever registers a type
    #: is the person who knows where it belongs, and they are editing this file
    #: anyway. Defaulted so registering one never has to think about it —
    #: ``"other"`` is a usable answer and the builder renders that drawer only
    #: when something lands in it.
    group: str = "other"

    #: Handles with no id — ``default``, ``timeout``, ``error``, ``cond:true``…
    handles: tuple[str, ...] = ("default",)

    #: ``(prefix, config key)`` pairs whose ids come from a list in the config:
    #: ``("btn", "buttons")`` means ``btn:<id>`` is valid for every id in
    #: ``config["buttons"]``.
    dynamic_handles: tuple[tuple[str, str], ...] = ()

    #: A terminal node ends the execution in-graph, so an outgoing edge from it
    #: is unreachable rather than merely unused (SPEC §11.3).
    terminal: bool = False

    #: Builder-only annotation with no runtime (SPEC §11.11). It takes part in
    #: no routing at all, which is why it may not be an edge endpoint and why
    #: entry-node detection ignores it.
    annotation: bool = False

    #: Extra ``$defs`` this node contributes to the exported document.
    defs: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: Config keys whose value is **author-written HTML** rather than text.
    #:
    #: Declared here because storing markup changes who has to be careful about
    #: it. Every other config string is escaped wherever it is rendered; a field
    #: named here is markup by design, so it is normalised through an allowlist
    #: on the way *in* (``apps.flows.schema.sanitize.sanitize_graph``, called by
    #: ``services.save_draft``) instead. Anything that then reads it — the email
    #: adapter, the builder's editor — is reading a document that has already
    #: been through the allowlist.
    #:
    #: Empty for every node but ``send_email``, and it should stay that way
    #: unless a node genuinely needs to store markup.
    html_fields: tuple[str, ...] = ()


#: The palette drawers, in the order the builder shows them (issue #10). Both
#: the order and the labels are exported, so the frontend reads one file rather
#: than one file plus a hard-coded table.
GROUPS: tuple[tuple[str, str], ...] = (
    ("content", "Content"),
    ("logic", "Logic"),
    ("actions", "Actions"),
    ("other", "Other"),
)

NODE_TYPES: dict[str, NodeSpec] = {}
SHARED_DEFS: dict[str, dict[str, Any]] = {}
#: Contract 5's action-verb registry: verb → the ``$defs`` name of its schema.
ACTION_VERBS: dict[str, str] = {}


def register_defs(**defs: dict[str, Any]) -> None:
    """Add named schema fragments to the exported document's ``$defs``."""
    for name, schema in defs.items():
        if name in SHARED_DEFS and SHARED_DEFS[name] != schema:
            raise ValueError(f"$defs entry {name!r} is already registered with a different schema.")
        SHARED_DEFS[name] = schema


def register_node_type(spec: NodeSpec) -> NodeSpec:
    """Register a node type. Additive: re-registering a type is an error."""
    if spec.type in NODE_TYPES:
        raise ValueError(f"Node type {spec.type!r} is already registered.")
    NODE_TYPES[spec.type] = spec
    if spec.defs:
        register_defs(**spec.defs)
    return spec


def register_action_verb(verb: str, def_name: str, schema: dict[str, Any]) -> None:
    """Register an action-node verb (ROADMAP contract 5).

    The action node's config schema reads :data:`ACTION_VERBS` when the document
    is exported, so a verb registered before export appears in both the server's
    validation and the builder's panel with no further wiring.
    """
    if verb in ACTION_VERBS:
        raise ValueError(f"Action verb {verb!r} is already registered.")
    register_defs(**{def_name: schema})
    ACTION_VERBS[verb] = def_name


def all_defs() -> dict[str, dict[str, Any]]:
    """Every ``$defs`` entry the exported document carries, assembled fresh.

    ``action_step`` is derived here rather than registered: it is the union of
    whatever :data:`ACTION_VERBS` holds *at export time*, which is what makes a
    verb registered by a later issue appear in the schema without this module
    changing (contract 5).

    A ``CONDITION_SCHEMA`` that arrives carrying its own ``$defs`` — issue #3's
    version may — is flattened into the document root, because a ``$ref`` inside
    an embedded fragment resolves against the root of the document it ends up
    in, not against the fragment it was written in. Colliding names are a hard
    error rather than a silent overwrite.
    """
    defs = dict(SHARED_DEFS)
    condition = defs.get("condition_filter")
    if isinstance(condition, dict) and "$defs" in condition:
        hoisted = condition["$defs"]
        defs["condition_filter"] = {key: value for key, value in condition.items() if key != "$defs"}
        for name, schema in (hoisted or {}).items():
            if name in defs and defs[name] != schema:
                raise ValueError(
                    f"CONDITION_SCHEMA's $defs entry {name!r} collides with this app's. "
                    f"Rename one; see apps/flows/schema/nodes.py::all_defs."
                )
            defs[name] = schema
    defs["action_step"] = f.tagged_union("verb", dict(ACTION_VERBS))
    return defs


def node_spec(node_type: object) -> NodeSpec | None:
    """The spec for a node type, or ``None`` if nothing registers it."""
    if not isinstance(node_type, str):
        return None
    return NODE_TYPES.get(node_type)


def handles_for_node(spec: NodeSpec, config: Any) -> set[str]:
    """Every handle this node actually exposes, static and config-derived."""
    available = set(spec.handles)
    if not isinstance(config, dict):
        return available
    for prefix, config_key in spec.dynamic_handles:
        for item in config.get(config_key) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                available.add(f"{prefix}:{item['id']}")
    return available


# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------

# SPEC §11.1's card: the same four fields whether it stands alone or sits in a
# gallery. Written once in Python and spread into both fragments, so the two can
# never describe different cards.
_CARD_PROPERTIES: dict[str, dict[str, Any]] = {
    "image": f.string(min_length=1, max_length=2000, description="Media library id or URL."),
    "title": f.string(min_length=1, max_length=200),
    "subtitle": f.string(max_length=400),
    "url_button": f.ref("url_button"),
}

register_defs(
    url_button=f.obj(
        {"label": f.string(min_length=1, max_length=80), "url": f.string(min_length=1, max_length=2000)},
        required=["label", "url"],
    ),
    gallery_card=f.obj(_CARD_PROPERTIES, required=["title"]),
    block_text=f.obj(
        {"type": f.const("text"), "text": f.string(min_length=1, max_length=4096)},
        required=["type", "text"],
    ),
    block_media={
        **f.obj(
            {
                "type": f.enum("image", "audio", "video", "file"),
                "media_id": f.string(min_length=1, max_length=64, description="Media library asset id (#16)."),
                "url": f.string(min_length=1, max_length=2000),
                "caption": f.string(max_length=1024),
            },
            required=["type"],
        ),
        # SPEC §11.1: "image/audio/video/file by media library id or URL". One
        # of the two has to be there, which `required` alone cannot say.
        "anyOf": [{"required": ["media_id"]}, {"required": ["url"]}],
    },
    block_card=f.obj({"type": f.const("card"), **_CARD_PROPERTIES}, required=["type", "title"]),
    block_gallery=f.obj(
        {"type": f.const("gallery"), "cards": f.array(f.ref("gallery_card"), min_items=1, max_items=10)},
        required=["type", "cards"],
    ),
    message_block=f.tagged_union(
        "type",
        {
            "text": "block_text",
            "image": "block_media",
            "audio": "block_media",
            "video": "block_media",
            "file": "block_media",
            "card": "block_card",
            "gallery": "block_gallery",
        },
    ),
    message_button_url=f.obj(
        {
            "id": f.string(min_length=1, max_length=64, description="The id in this button's btn:<id> handle."),
            "label": f.string(min_length=1, max_length=80),
            "action": f.const("url"),
            "url": f.string(min_length=1, max_length=2000),
        },
        required=["id", "label", "action", "url"],
    ),
    message_button_postback=f.obj(
        {
            "id": f.string(min_length=1, max_length=64, description="The id in this button's btn:<id> handle."),
            "label": f.string(min_length=1, max_length=80),
            "action": f.const("postback"),
        },
        required=["id", "label", "action"],
    ),
    message_button=f.tagged_union("action", {"url": "message_button_url", "postback": "message_button_postback"}),
    quick_reply=f.obj(
        {
            "id": f.string(min_length=1, max_length=64, description="The id in this reply's qr:<id> handle."),
            "label": f.string(min_length=1, max_length=80),
        },
        required=["id", "label"],
    ),
    followup=f.obj(
        {
            "enabled": f.boolean(),
            "delay": f.integer(minimum=1, maximum=10_000),
            "unit": f.enum("minutes", "hours", "days"),
        },
        required=["enabled"],
        description="Routes to the `timeout` handle when the wait expires (SPEC §11.1).",
    ),
    retry_unmatched=f.obj(
        {
            "enabled": f.boolean(),
            # SPEC §11.1 caps this at 5.
            "max": f.integer(minimum=1, maximum=5),
            "text": f.string(max_length=1024),
        },
        required=["enabled"],
    ),
    # SPEC §6.5, added by issue #19. One value for one of an approved
    # template's {{n}} slots. ``value`` is authored text and may itself contain
    # {{placeholders}} — the flow engine renders it through the shared,
    # engine-free substitution before the adapter ever sees it
    # (SECURITY-BASELINE §3).
    whatsapp_template_variable=f.obj(
        {
            "slot": {
                **f.string(min_length=1, max_length=32, description="header.1, body.2, button.0.1 …"),
                "pattern": r"^(header|body|button\.[0-9]{1,2})\.[0-9]{1,3}$",
            },
            "value": f.string(max_length=1024),
        },
        required=["slot", "value"],
    ),
    # The template-picker variant of send_message (SPEC §6.5). Present only on
    # nodes an author pointed at a WhatsApp channel; every other platform's
    # adapter ignores it, which is why this is one optional key rather than a
    # second node type.
    #
    # ``reference`` rather than the picked row's id is what reaches the wire:
    # ``<name>/<language>`` is the Cloud API's own key for a template, so a
    # queued message retried after somebody deleted the row still says what it
    # was sending. ``template_id`` is carried alongside so the builder can
    # re-open the picker on the right row and re-derive the slot list.
    whatsapp_template=f.obj(
        {
            "template_id": f.string(min_length=1, max_length=64),
            "reference": {
                **f.string(min_length=3, max_length=600, description="<name>/<language>, e.g. order_shipped/en_US."),
                "pattern": r"^[a-z0-9_]{1,512}/[A-Za-z_]{2,10}$",
            },
            "variables": f.array(f.ref("whatsapp_template_variable"), max_items=20),
        },
        required=["reference"],
    ),
    condition_filter=CONDITION_SCHEMA,
    continue_window=f.obj(
        {
            "enabled": f.boolean(),
            "days": f.array(f.enum("mon", "tue", "wed", "thu", "fri", "sat", "sun"), max_items=7),
            "from": {**f.string(description="Local time, HH:MM."), "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
            "to": {**f.string(description="Local time, HH:MM."), "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$"},
            "use_contact_timezone": f.boolean(),
        },
        required=["enabled"],
    ),
    smart_delay_duration=f.obj(
        {
            "mode": f.const("duration"),
            "duration": f.obj(
                {"value": f.integer(minimum=1, maximum=100_000), "unit": f.enum("minutes", "hours", "days")},
                required=["value", "unit"],
            ),
            "continue_window": f.ref("continue_window"),
        },
        required=["mode", "duration"],
    ),
    smart_delay_date=f.obj(
        {
            "mode": f.const("date"),
            "date": {
                **f.obj(
                    {
                        "field": f.string(min_length=1, max_length=200, description="A date/datetime field."),
                        "datetime": f.string(min_length=1, max_length=64, description="ISO-8601 instant."),
                    },
                ),
                # SPEC §11.5: "date {field or fixed datetime}" — one of the two
                # has to be there, which `required` alone cannot express.
                "anyOf": [{"required": ["field"]}, {"required": ["datetime"]}],
            },
            "continue_window": f.ref("continue_window"),
        },
        required=["mode", "date"],
    ),
    randomizer_path=f.obj(
        {
            "id": f.string(min_length=1, max_length=64, description="The id in this path's rand:<id> handle."),
            "weight": f.integer(minimum=0, maximum=100, description="Percent."),
        },
        required=["id", "weight"],
    ),
    http_header=f.obj(
        {"name": f.string(min_length=1, max_length=128), "value": f.string(max_length=2048)},
        required=["name", "value"],
    ),
    response_mapping=f.obj(
        {
            "json_path": f.string(min_length=1, max_length=200),
            "target_type": f.enum("custom_field", "variable"),
            "target": f.string(min_length=1, max_length=200),
        },
        required=["json_path", "target_type", "target"],
    ),
)


# ---------------------------------------------------------------------------
# Action verbs (SPEC §11.2) — contract 5's registry, populated with the verbs
# the specification already names. L6-A wires the sequence verbs to a runtime;
# their schema ships now so the builder can offer them from day one.
# ---------------------------------------------------------------------------


def _verb(name: str, properties: dict[str, dict[str, Any]], required: list[str]) -> dict[str, Any]:
    return f.obj({"verb": f.const(name), **properties}, required=["verb", *required])


for _verb_name, _verb_schema in (
    ("add_tag", _verb("add_tag", {"tag": f.string(min_length=1, max_length=200)}, ["tag"])),
    ("remove_tag", _verb("remove_tag", {"tag": f.string(min_length=1, max_length=200)}, ["tag"])),
    (
        "set_field",
        _verb(
            "set_field",
            {
                "field": f.string(min_length=1, max_length=200),
                "value": f.string(max_length=4096, description="May carry {{placeholders}} (SECURITY-BASELINE §3)."),
            },
            ["field", "value"],
        ),
    ),
    ("clear_field", _verb("clear_field", {"field": f.string(min_length=1, max_length=200)}, ["field"])),
    (
        "subscribe_sequence",
        _verb("subscribe_sequence", {"sequence": f.string(min_length=1, max_length=64)}, ["sequence"]),
    ),
    (
        "unsubscribe_sequence",
        _verb("unsubscribe_sequence", {"sequence": f.string(min_length=1, max_length=64)}, ["sequence"]),
    ),
    ("open_conversation", _verb("open_conversation", {}, [])),
    ("close_conversation", _verb("close_conversation", {}, [])),
    (
        "assign_conversation",
        _verb("assign_conversation", {"member": f.string(min_length=1, max_length=64)}, ["member"]),
    ),
    (
        "notify_members",
        _verb(
            "notify_members",
            {
                "member_ids": f.array(f.string(min_length=1, max_length=64), min_items=1, max_items=50),
                "via": f.enum("in_app", "email"),
                "text": f.string(min_length=1, max_length=4096),
            },
            ["member_ids", "via", "text"],
        ),
    ),
):
    register_action_verb(_verb_name, f"action_{_verb_name}", _verb_schema)


# ---------------------------------------------------------------------------
# The node types (SPEC §11)
# ---------------------------------------------------------------------------

register_node_type(
    NodeSpec(
        type="send_message",
        label="Send Message",
        description="SPEC §11.1. Waits when buttons or quick replies are present, otherwise continues.",
        group="content",
        config=f.obj(
            {
                "blocks": f.array(f.ref("message_block"), min_items=1, max_items=20),
                "buttons": f.array(f.ref("message_button"), max_items=20),
                "quick_replies": f.array(f.ref("quick_reply"), max_items=20),
                "followup": f.ref("followup"),
                "retry_unmatched": f.ref("retry_unmatched"),
                # Additive, from issue #19. Outside WhatsApp's 24-hour window a
                # send needs an approved template and nothing else will do
                # (SPEC §6.5); this is where a flow author picks one. The
                # compliance engine still decides whether it is *needed* — this
                # only supplies it.
                "whatsapp_template": f.ref("whatsapp_template"),
            },
            required=["blocks"],
        ),
        handles=("default", "timeout"),
        dynamic_handles=(("btn", "buttons"), ("qr", "quick_replies")),
    )
)

register_node_type(
    NodeSpec(
        type="action",
        label="Action",
        description="SPEC §11.2. Runs its verbs in order and always continues.",
        group="actions",
        # The verb union is built at export time from ACTION_VERBS, so a verb a
        # later issue registers appears without this line changing.
        config=f.obj({"actions": f.array(f.ref("action_step"), min_items=1, max_items=20)}, required=["actions"]),
        handles=("default",),
    )
)

register_node_type(
    NodeSpec(
        type="start_flow",
        label="Start Flow",
        description="SPEC §11.3. Terminal in-graph: it ends this execution and starts the target flow.",
        group="logic",
        config=f.obj({"flow_id": f.string(min_length=1, max_length=64)}, required=["flow_id"]),
        handles=(),
        terminal=True,
    )
)

register_node_type(
    NodeSpec(
        type="condition",
        label="Condition",
        description="SPEC §11.4. The filter is contract 8's CONDITION_SCHEMA, embedded, not re-declared.",
        group="logic",
        config=f.ref("condition_filter"),
        handles=("cond:true", "cond:false"),
    )
)

register_node_type(
    NodeSpec(
        type="smart_delay",
        label="Smart Delay",
        description="SPEC §11.5. Schedules a resume, adjusted into the next allowed window.",
        group="logic",
        # Discriminated on `mode` rather than a flat object with everything
        # optional. With only `mode` required, {"mode": "duration"} published
        # cleanly and reached the engine with nothing to compute run_at from —
        # and a date-mode node could carry a `duration` block that would never
        # be read. Each branch now requires its own payload and rejects the
        # other's.
        config=f.tagged_union("mode", {"duration": "smart_delay_duration", "date": "smart_delay_date"}),
        handles=("default",),
    )
)

register_node_type(
    NodeSpec(
        type="randomizer",
        label="Randomizer",
        description="SPEC §11.6. Splits by weight; sticky by default, remembered in variables.",
        group="logic",
        config=f.obj(
            {
                "paths": f.array(f.ref("randomizer_path"), min_items=2, max_items=10),
                "sticky": f.boolean(description="Default true (SPEC §11.6)."),
            },
            required=["paths"],
        ),
        handles=(),
        dynamic_handles=(("rand", "paths"),),
    )
)

register_node_type(
    NodeSpec(
        type="external_request",
        label="External Request",
        description=(
            "SPEC §11.7. Runtime is L4-E and goes through the shared SSRF guard "
            "(SECURITY-BASELINE §6); nothing in this app fetches the URL."
        ),
        group="actions",
        config=f.obj(
            {
                "method": f.enum("GET", "POST", "PUT", "PATCH", "DELETE"),
                "url": f.string(min_length=1, max_length=2000),
                "headers": f.array(f.ref("http_header"), max_items=20),
                "body": f.any_json(description="JSON body template; placeholders are substituted, never evaluated."),
                # SPEC §11.7 caps the timeout at 10 seconds.
                "timeout_s": f.integer(minimum=1, maximum=10),
                "response_mappings": f.array(f.ref("response_mapping"), max_items=20),
                "fallback_handle_on_error": f.boolean(),
            },
            required=["method", "url"],
        ),
        handles=("default", "error"),
    )
)

register_node_type(
    NodeSpec(
        type="data_collection",
        label="Data Collection",
        description="SPEC §11.8. Validated reply capture; email/phone answers also record consent.",
        group="content",
        config=f.obj(
            {
                "question": f.string(min_length=1, max_length=4096),
                "reply_type": f.enum("text", "email", "phone", "number", "date", "url"),
                "target": f.obj(
                    {
                        "type": f.enum("custom_field", "system_field"),
                        "key": f.string(min_length=1, max_length=200),
                    },
                    required=["type", "key"],
                ),
                "retry": f.obj(
                    # SPEC §11.8 caps retries at 3.
                    {"max": f.integer(minimum=1, maximum=3), "invalid_text": f.string(max_length=1024)},
                ),
                "timeout": f.obj(
                    {
                        "enabled": f.boolean(),
                        "delay": f.integer(minimum=1, maximum=10_000),
                        "unit": f.enum("minutes", "hours", "days"),
                    },
                    required=["enabled"],
                ),
            },
            required=["question", "reply_type", "target"],
        ),
        handles=("default", "timeout"),
    )
)

register_node_type(
    NodeSpec(
        type="send_sms",
        label="Send SMS",
        description="SPEC §11.9. Runtime is L5-D. Needs an SMS connection and a phone identity.",
        group="actions",
        config=f.obj(
            {
                "text": f.string(min_length=1, max_length=1600),
                "media_url": f.string(min_length=1, max_length=2000),
            },
            required=["text"],
        ),
        handles=("default", "error"),
    )
)

register_node_type(
    NodeSpec(
        type="send_email",
        label="Send Email",
        description="SPEC §11.10. Runtime is L5-E. Needs an email connection and an email identity.",
        group="actions",
        config=f.obj(
            {
                "subject": f.string(min_length=1, max_length=300),
                "html_body": f.string(min_length=1, max_length=100_000),
                "from_override": f.string(max_length=320),
            },
            required=["subject", "html_body"],
        ),
        handles=("default", "error"),
        # The one field in the product that stores markup. See `html_fields`.
        html_fields=("html_body",),
    )
)

register_node_type(
    NodeSpec(
        type="note",
        label="Note",
        description="SPEC §11.11. Builder-only annotation, ignored at runtime and never connected.",
        group="content",
        config=f.obj({"text": f.string(max_length=5000)}, required=["text"]),
        handles=(),
        annotation=True,
    )
)
