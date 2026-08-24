"""Every workspace-local reference a flow can hold, found by one walk.

A graph is portable; the ids inside it are not. ``add_tag`` names a tag,
``subscribe_sequence`` names a sequence row, a condition rule names a segment,
a ``send_message`` block names a media asset, ``assign_conversation`` names a
member — and each of those means something only in the workspace that owns it.
Moving a flow between installations is therefore not a copy, it is a
*translation*, and this module is its dictionary.

--------------------------------------------------------------------------
One walk, two directions
--------------------------------------------------------------------------

:func:`rewrite_graph` and :func:`rewrite_trigger_config` take a ``visit``
callback and return a **new** document with whatever the callback returned
substituted at each reference site. Export passes a callback that mints
synthetic references; import passes one that resolves them back. The two
directions therefore share one definition of "what counts as a reference",
which is the whole reason this is a callback rather than two hand-written
serialisers: a site the exporter scrubbed and the importer did not know about
is exactly the dangling id this feature exists to prevent.

Nothing here touches the database or the ORM. It is pure document surgery, so
it runs on an untrusted document before anything has been resolved.

--------------------------------------------------------------------------
Two addressing conventions, taken from the schema
--------------------------------------------------------------------------

``apps/flows/engine/nodes/action.py`` states the rule the registry already
encodes: ``tag`` and ``field`` are 200-character strings holding **names**;
``member``, ``sequence`` and ``flow_id`` are 64-character strings holding
**ids**. A name travels between workspaces on its own and only needs to be
*offered* for renaming; an id has to be replaced. :attr:`Site.addressing`
carries which one a site is, so a caller never has to re-derive it from the
field's length limit.

--------------------------------------------------------------------------
Synthetic references
--------------------------------------------------------------------------

An exported id becomes ``synthetic_ref(kind, ordinal)`` — a UUIDv5 over a fixed
namespace. A UUID rather than a readable token because the fields disagree
about what they accept: ``rule.tag_id`` and ``rule.field_id`` are
``pattern``-constrained to a UUID in :mod:`apps.flows.triggers.schema`, while
``media_id`` and friends are merely bounded strings. A UUID satisfies all of
them, so **an exported document still validates against the unmodified graph
and trigger schemas** — which is what lets the importer run the existing
``validate_document`` and ``validate_config`` before it touches the ORM.

The ordinal is the object's position in first-appearance order, not a hash of
its name. That is what makes a round trip byte-stable: the second export mints
the same references in the same order whatever the importing workspace decided
to call things.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

__all__ = [
    "ADDRESS_ID",
    "ADDRESS_NAME",
    "KIND_COMMENT_POSTS",
    "KIND_CUSTOM_FIELD",
    "KIND_FLOW",
    "KIND_FROM_OVERRIDE",
    "KIND_LINK_HANDLE",
    "KIND_MEDIA",
    "KIND_MEMBER",
    "KIND_REQUEST_HEADER",
    "KIND_SEGMENT",
    "KIND_SEQUENCE",
    "KIND_TAG",
    "KIND_WHATSAPP_TEMPLATE",
    "NAMED_KINDS",
    "REMOVE",
    "RESOLVED_KINDS",
    "STRIPPED_KINDS",
    "AsUrl",
    "Site",
    "Visit",
    "is_uuid",
    "rewrite_graph",
    "rewrite_trigger_config",
    "synthetic_ref",
]

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

KIND_TAG = "tag"
KIND_CUSTOM_FIELD = "custom_field"
KIND_SEQUENCE = "sequence"
KIND_SEGMENT = "segment"
KIND_MEMBER = "member"
KIND_FLOW = "flow"
KIND_MEDIA = "media"
KIND_REQUEST_HEADER = "request_header"
KIND_WHATSAPP_TEMPLATE = "whatsapp_template"
KIND_LINK_HANDLE = "link_handle"
KIND_FROM_OVERRIDE = "from_override"
KIND_COMMENT_POSTS = "comment_posts"

#: Kinds whose site holds an id that has to be **translated** into the target
#: workspace's id. Export mints a synthetic reference; import resolves it.
RESOLVED_KINDS: frozenset[str] = frozenset(
    {KIND_TAG, KIND_CUSTOM_FIELD, KIND_SEQUENCE, KIND_SEGMENT, KIND_MEMBER, KIND_FLOW, KIND_MEDIA}
)

#: Kinds that can also be addressed by **name**, which travels as-is.
NAMED_KINDS: frozenset[str] = frozenset({KIND_TAG, KIND_CUSTOM_FIELD})

#: Kinds that are **removed** on export rather than translated, because the
#: value is a credential, an account handle or a platform id belonging to the
#: exporter. Each still produces a requirement, so the mapping step can offer
#: to refill it — a blank that nobody is told about is the same silent hole as
#: a dangling id.
STRIPPED_KINDS: frozenset[str] = frozenset(
    {KIND_REQUEST_HEADER, KIND_WHATSAPP_TEMPLATE, KIND_LINK_HANDLE, KIND_FROM_OVERRIDE, KIND_COMMENT_POSTS}
)

ADDRESS_ID = "id"
ADDRESS_NAME = "name"


class _Remove:
    """Sentinel: delete this key rather than writing a value into it."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "REMOVE"


#: Returned by a ``visit`` callback to drop the key entirely. Distinct from
#: ``None`` and from ``""``, both of which are values a schema may accept and a
#: caller may legitimately want to write.
REMOVE = _Remove()


@dataclass(frozen=True)
class AsUrl:
    """A media site's answer: "store this URL instead of a library id".

    SPEC §11.1 lets a media block carry an id **or** a URL, but they are not the
    same key — a card holds both in ``image`` while a media block has separate
    ``media_id`` and ``url``. A visitor cannot express "drop this key and set
    that one" by returning a value, so it returns this and the block rewriter,
    which is the only code that knows the block's shape, does the swap.

    It is what lets a template with a picture import into a workspace whose
    media library is empty: the mapping step accepts a URL where it cannot offer
    an asset, and the flow arrives complete rather than dangling.
    """

    url: str


@dataclass(frozen=True)
class Site:
    """One place in a document where a workspace-local reference sits."""

    kind: str
    #: What is there now — a raw id on the way out, a synthetic ref on the way in.
    value: Any
    #: ``ADDRESS_ID`` or ``ADDRESS_NAME``; see the module docstring.
    addressing: str
    #: Where it is, for the manifest's "used by" and for error messages.
    #: Deterministic and free of workspace data: node ids and list indices only.
    path: str
    node_id: str | None = None
    #: Extra, kind-specific context a caller needs and cannot re-derive from the
    #: path — the header's name, a WhatsApp template's ``<name>/<language>``.
    detail: str = ""


#: A callback returning the replacement value, or :data:`REMOVE`.
Visit = Callable[[Site], Any]


#: Namespace for :func:`synthetic_ref`. A fixed, published constant rather than
#: a random one: two exports of the same flow must mint the same references, and
#: a namespace regenerated per process would break that on the first restart.
SYNTHETIC_NAMESPACE = uuid5(NAMESPACE_URL, "https://brightbean.chat/schemas/flow-template-ref")


def synthetic_ref(kind: str, ordinal: int) -> str:
    """The placeholder id standing in for the ``ordinal``-th ``kind`` reference."""
    return str(uuid5(SYNTHETIC_NAMESPACE, f"{kind}:{ordinal}"))


def is_uuid(value: Any) -> bool:
    """Whether ``value`` is a string in UUID form.

    The test :func:`apps.flows.engine.nodes.send_message._is_media_id` applies,
    reproduced rather than imported: a card's ``image`` is "media library id or
    URL" in one field (SPEC §11.1), and which one it is decides whether it is a
    reference at all. Importing the engine here would pull the whole runtime
    into the exporter for a four-line predicate.
    """
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# The graph walk
# --------------------------------------------------------------------------

#: A condition rule's ``source`` → the kind its ``key`` names. ``system_field``
#: and ``window`` are absent on purpose: their keys are an allowlisted column
#: name and a platform, neither of which is workspace-local. The four listed
#: here are ``KEY_UUID`` sources in ``apps.contacts.conditions``.
_CONDITION_SOURCE_KINDS: dict[str, str] = {
    "tag": KIND_TAG,
    "custom_field": KIND_CUSTOM_FIELD,
    "segment": KIND_SEGMENT,
    "sequence": KIND_SEQUENCE,
}

#: Action verbs whose payload is one reference: verb → (config key, kind, addressing).
#:
#: The verb travels on the site as :attr:`Site.detail`, because two verbs can
#: name the same kind of object and mean opposite things — a bundle follows the
#: flows a ``subscribe_sequence`` will run and must **not** follow the ones an
#: ``unsubscribe_sequence`` merely removes somebody from.
_VERB_SITES: dict[str, tuple[str, str, str]] = {
    "add_tag": ("tag", KIND_TAG, ADDRESS_NAME),
    "remove_tag": ("tag", KIND_TAG, ADDRESS_NAME),
    "set_field": ("field", KIND_CUSTOM_FIELD, ADDRESS_NAME),
    "clear_field": ("field", KIND_CUSTOM_FIELD, ADDRESS_NAME),
    "subscribe_sequence": ("sequence", KIND_SEQUENCE, ADDRESS_ID),
    "unsubscribe_sequence": ("sequence", KIND_SEQUENCE, ADDRESS_ID),
    "assign_conversation": ("member", KIND_MEMBER, ADDRESS_ID),
}


def rewrite_graph(graph: Any, visit: Visit) -> Any:
    """``graph`` with every reference site replaced by ``visit``'s answer.

    Defensive about shape throughout, because it runs on documents that have
    not been validated yet as well as on ones that have. Anything that is not
    the shape a site needs is copied through untouched and left for the
    validator to complain about — a rewriter that raised on a malformed graph
    would turn a validation finding into a 500.
    """
    if not isinstance(graph, dict):
        return graph
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        return graph
    return {**graph, "nodes": [_rewrite_node(node, visit) for node in nodes]}


def _rewrite_node(node: Any, visit: Visit) -> Any:
    if not isinstance(node, dict):
        return node
    config = node.get("config")
    node_id = node.get("id") if isinstance(node.get("id"), str) else None
    node_type = node.get("type")

    if node_type == "condition":
        # The condition node's config *is* the filter document (ROADMAP
        # contract 8): it embeds CONDITION_SCHEMA rather than wrapping it.
        rewritten: Any = _rewrite_filter(config, visit, "config", node_id)
    elif not isinstance(config, dict):
        return node
    elif node_type == "action":
        rewritten = _rewrite_action(config, visit, node_id)
    elif node_type == "start_flow":
        rewritten = _apply(config, "flow_id", visit, KIND_FLOW, ADDRESS_ID, "config.flow_id", node_id)
    elif node_type == "send_message":
        rewritten = _rewrite_send_message(config, visit, node_id)
    elif node_type == "data_collection":
        rewritten = _rewrite_data_collection(config, visit, node_id)
    elif node_type == "external_request":
        rewritten = _rewrite_external_request(config, visit, node_id)
    elif node_type == "send_email":
        rewritten = _apply(
            config, "from_override", visit, KIND_FROM_OVERRIDE, ADDRESS_NAME, "config.from_override", node_id
        )
    else:
        return node

    return node if rewritten is config else {**node, "config": rewritten}


def _rewrite_action(config: dict[str, Any], visit: Visit, node_id: str | None) -> dict[str, Any]:
    steps = config.get("actions")
    if not isinstance(steps, list):
        return config
    rewritten = [_rewrite_step(step, visit, index, node_id) for index, step in enumerate(steps)]
    if all(new is old for new, old in zip(rewritten, steps, strict=True)):
        return config
    return {**config, "actions": rewritten}


def _rewrite_step(step: Any, visit: Visit, index: int, node_id: str | None) -> Any:
    if not isinstance(step, dict):
        return step
    verb = step.get("verb")
    path = f"config.actions[{index}]"

    site = _VERB_SITES.get(verb) if isinstance(verb, str) else None
    if site is not None:
        key, kind, addressing = site
        return _apply(step, key, visit, kind, addressing, f"{path}.{key}", node_id, detail=str(verb))

    if verb == "notify_members":
        members = step.get("member_ids")
        if not isinstance(members, list):
            return step
        replaced = []
        for position, member in enumerate(members):
            answer = visit(
                Site(
                    kind=KIND_MEMBER,
                    value=member,
                    addressing=ADDRESS_ID,
                    path=f"{path}.member_ids[{position}]",
                    node_id=node_id,
                )
            )
            if answer is not REMOVE:
                replaced.append(answer)
        if replaced == members:
            return step
        return {**step, "member_ids": replaced}

    return step


def _rewrite_send_message(config: dict[str, Any], visit: Visit, node_id: str | None) -> dict[str, Any]:
    updates: dict[str, Any] = {}

    blocks = config.get("blocks")
    if isinstance(blocks, list):
        rewritten = [_rewrite_block(block, visit, index, node_id) for index, block in enumerate(blocks)]
        if any(new is not old for new, old in zip(rewritten, blocks, strict=True)):
            updates["blocks"] = rewritten

    template = config.get("whatsapp_template")
    if isinstance(template, dict):
        # A site is emitted whenever the template block is present, **even when
        # ``template_id`` is absent**. The id is a row in the exporter's
        # workspace and is dropped on the way out; ``reference`` —
        # ``<name>/<language>``, the Cloud API's own key — is what reaches the
        # wire (SPEC §6.5) and survives. Keying the site on the reference rather
        # than on the id is what makes the requirement reproducible: a document
        # exported, imported and exported again names the same template both
        # times, which an id-keyed site could not do after the id was stripped.
        raw_reference = template.get("reference")
        reference = raw_reference if isinstance(raw_reference, str) else ""
        # `""` for an absent id, and the comparison below uses the same default:
        # a visitor that hands back what it was given must not be able to turn
        # "no template_id" into `template_id: ""`, which the schema refuses.
        current = template.get("template_id", "")
        answer = visit(
            Site(
                kind=KIND_WHATSAPP_TEMPLATE,
                value=current,
                addressing=ADDRESS_ID,
                path="config.whatsapp_template.template_id",
                node_id=node_id,
                detail=reference,
            )
        )
        if answer is REMOVE:
            if "template_id" in template:
                updates["whatsapp_template"] = {key: value for key, value in template.items() if key != "template_id"}
        elif answer != current:
            updates["whatsapp_template"] = {**template, "template_id": answer}

    return {**config, **updates} if updates else config


def _rewrite_block(block: Any, visit: Visit, index: int, node_id: str | None) -> Any:
    if not isinstance(block, dict):
        return block
    path = f"config.blocks[{index}]"

    if block.get("type") == "gallery":
        cards = block.get("cards")
        if not isinstance(cards, list):
            return block
        rewritten = [
            _rewrite_card_image(card, visit, f"{path}.cards[{position}]", node_id)
            for position, card in enumerate(cards)
        ]
        if all(new is old for new, old in zip(rewritten, cards, strict=True)):
            return block
        return {**block, "cards": rewritten}

    if block.get("type") == "card":
        return _rewrite_card_image(block, visit, path, node_id)

    if "media_id" in block:
        answer = visit(
            Site(
                kind=KIND_MEDIA,
                value=block["media_id"],
                addressing=ADDRESS_ID,
                path=f"{path}.media_id",
                node_id=node_id,
            )
        )
        if isinstance(answer, AsUrl):
            stripped = {key: value for key, value in block.items() if key != "media_id"}
            return {**stripped, "url": answer.url}
        if answer is REMOVE:
            return {key: value for key, value in block.items() if key != "media_id"}
        return block if answer == block["media_id"] else {**block, "media_id": answer}

    return block


def _rewrite_card_image(card: Any, visit: Visit, path: str, node_id: str | None) -> Any:
    """A card's ``image`` is a media id **or** a URL in one field (SPEC §11.1).

    Only the id form is a reference. A URL is author content and travels as it
    is written — with the one exception the exporter enforces, which is that a
    signed media-delivery URL never becomes one in the first place, because a
    library asset is stored as ``media_id`` and resolved at send time.
    """
    if not isinstance(card, dict) or not is_uuid(card.get("image")):
        return card
    answer = visit(
        Site(kind=KIND_MEDIA, value=card["image"], addressing=ADDRESS_ID, path=f"{path}.image", node_id=node_id)
    )
    if isinstance(answer, AsUrl):
        answer = answer.url
    if answer is REMOVE:
        return {key: value for key, value in card.items() if key != "image"}
    return card if answer == card["image"] else {**card, "image": answer}


def _rewrite_data_collection(config: dict[str, Any], visit: Visit, node_id: str | None) -> dict[str, Any]:
    target = config.get("target")
    if not isinstance(target, dict) or target.get("type") != "custom_field":
        return config
    replaced = _apply(target, "key", visit, KIND_CUSTOM_FIELD, ADDRESS_NAME, "config.target.key", node_id)
    return config if replaced is target else {**config, "target": replaced}


def _rewrite_external_request(config: dict[str, Any], visit: Visit, node_id: str | None) -> dict[str, Any]:
    updates: dict[str, Any] = {}

    headers = config.get("headers")
    if isinstance(headers, list):
        rewritten = []
        for index, header in enumerate(headers):
            if not isinstance(header, dict):
                rewritten.append(header)
                continue
            raw_name = header.get("name")
            name = raw_name if isinstance(raw_name, str) else ""
            rewritten.append(
                _apply(
                    header,
                    "value",
                    visit,
                    KIND_REQUEST_HEADER,
                    ADDRESS_NAME,
                    f"config.headers[{index}].value",
                    node_id,
                    detail=name,
                )
            )
        if any(new is not old for new, old in zip(rewritten, headers, strict=True)):
            updates["headers"] = rewritten

    mappings = config.get("response_mappings")
    if isinstance(mappings, list):
        rewritten_maps = []
        for index, mapping in enumerate(mappings):
            if not isinstance(mapping, dict) or mapping.get("target_type") != "custom_field":
                rewritten_maps.append(mapping)
                continue
            rewritten_maps.append(
                _apply(
                    mapping,
                    "target",
                    visit,
                    KIND_CUSTOM_FIELD,
                    ADDRESS_NAME,
                    f"config.response_mappings[{index}].target",
                    node_id,
                )
            )
        if any(new is not old for new, old in zip(rewritten_maps, mappings, strict=True)):
            updates["response_mappings"] = rewritten_maps

    return {**config, **updates} if updates else config


def _rewrite_filter(document: Any, visit: Visit, path: str, node_id: str | None) -> Any:
    """A condition filter's rules, wherever one is embedded.

    Two callers: the ``condition`` node, whose whole config is a filter, and the
    ``rule`` trigger, which carries one under ``filters``. One implementation,
    because a rule the node translates and the trigger does not would be a
    condition that means different things in the two places SPEC §11.4 says are
    the same language.
    """
    if not isinstance(document, dict):
        return document
    rules = document.get("rules")
    if not isinstance(rules, list):
        return document

    rewritten = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            rewritten.append(rule)
            continue
        source = rule.get("source")
        kind = _CONDITION_SOURCE_KINDS.get(source) if isinstance(source, str) else None
        if kind is None:
            rewritten.append(rule)
            continue
        rewritten.append(_apply(rule, "key", visit, kind, ADDRESS_ID, f"{path}.rules[{index}].key", node_id))

    if all(new is old for new, old in zip(rewritten, rules, strict=True)):
        return document
    return {**document, "rules": rewritten}


# --------------------------------------------------------------------------
# The trigger walk
# --------------------------------------------------------------------------


def rewrite_trigger_config(trigger_type: str, config: Any, visit: Visit) -> Any:
    """A trigger's ``config_json`` with its reference sites replaced.

    Triggers are half of what makes a graph a flow (SPEC §21 phase 3 asks for a
    round trip "incl. triggers"), and three of the ten types carry something
    workspace-local: ``rule`` names a tag and a field and embeds a filter,
    ``ref_url`` carries the account's public handle, and ``comment`` carries the
    exporter's own post ids.
    """
    if not isinstance(config, dict):
        return config

    if trigger_type == "rule":
        rewritten = _apply(config, "tag_id", visit, KIND_TAG, ADDRESS_ID, "config.tag_id", None)
        rewritten = _apply(rewritten, "field_id", visit, KIND_CUSTOM_FIELD, ADDRESS_ID, "config.field_id", None)
        filters = rewritten.get("filters")
        if isinstance(filters, dict):
            replaced = _rewrite_filter(filters, visit, "config.filters", None)
            if replaced is not filters:
                rewritten = {**rewritten, "filters": replaced}
        return rewritten

    if trigger_type == "ref_url":
        return _apply(config, "link_handle", visit, KIND_LINK_HANDLE, ADDRESS_NAME, "config.link_handle", None)

    if trigger_type == "comment":
        if "post_ids" not in config:
            return config
        answer = visit(
            Site(
                kind=KIND_COMMENT_POSTS,
                value=config.get("post_ids"),
                addressing=ADDRESS_ID,
                path="config.post_ids",
            )
        )
        if answer is REMOVE:
            return {key: value for key, value in config.items() if key != "post_ids"}
        return config if answer == config.get("post_ids") else {**config, "post_ids": answer}

    return config


# --------------------------------------------------------------------------
# The one substitution primitive
# --------------------------------------------------------------------------


def _apply(
    container: dict[str, Any],
    key: str,
    visit: Visit,
    kind: str,
    addressing: str,
    path: str,
    node_id: str | None,
    *,
    detail: str = "",
) -> dict[str, Any]:
    """Offer ``container[key]`` to ``visit`` and return the container it asked for.

    A key that is **absent** is not a site: an optional ``tag_id`` that nobody
    set means "any tag" (SPEC §10), and inventing a reference for it would turn
    a deliberate wildcard into a mapping question with no answer. A key that is
    present and empty *is* a site, so a caller can still see it and decide.

    Returns ``container`` itself when nothing changed, which is what lets every
    caller above use identity to decide whether to rebuild its parent.
    """
    if key not in container:
        return container
    answer = visit(
        Site(kind=kind, value=container[key], addressing=addressing, path=path, node_id=node_id, detail=detail)
    )
    if answer is REMOVE:
        return {name: value for name, value in container.items() if name != key}
    if answer == container[key]:
        return container
    return {**container, key: answer}
