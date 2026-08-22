"""One valid graph per node type — shared, not test-only.

The issue asks for "one valid graph per node type (fixtures reused by engine
tests)". L3-B's engine tests live in another app, so these live in an ordinary
module rather than under ``tests/``: importing across two apps' test packages
works until someone reorganises one of them, and a shared fixture that breaks
when an unrelated app is tidied is a shared fixture nobody trusts.

Every graph here is *valid* — no errors, no warnings, with the node under test
as the entry node and one edge out of each handle it exposes. That makes them
useful for more than validation: the engine can walk them, and a change that
quietly stops a node type exposing a handle turns a fixture red.
"""

from copy import deepcopy
from typing import Any

from apps.flows.schema import NODE_TYPES, empty_graph, handles_for_node

__all__ = ["NODE_CONFIGS", "button_heavy_graph", "graph_for", "node_fixture", "valid_graphs"]

#: A valid config for each node type in SPEC §11, exercising the optional keys
#: rather than the bare minimum — a fixture that only fills in required fields
#: proves the least interesting half of the schema.
NODE_CONFIGS: dict[str, dict[str, Any]] = {
    "send_message": {
        "blocks": [
            {"type": "text", "text": "Hi {{first_name}}, welcome aboard."},
            {"type": "image", "url": "https://example.test/welcome.png", "caption": "Welcome"},
        ],
        "buttons": [
            {"id": "b1", "label": "Read the docs", "action": "url", "url": "https://example.test/docs"},
            {"id": "b2", "label": "Talk to us", "action": "postback"},
        ],
        "quick_replies": [{"id": "q1", "label": "Later"}],
        "followup": {"enabled": True, "delay": 1, "unit": "hours"},
        "retry_unmatched": {"enabled": True, "max": 2, "text": "Please pick one of the options."},
    },
    "action": {
        "actions": [
            {"verb": "add_tag", "tag": "lead"},
            {"verb": "set_field", "field": "stage", "value": "contacted"},
            {"verb": "notify_members", "member_ids": ["m1"], "via": "in_app", "text": "New lead"},
        ]
    },
    "start_flow": {"flow_id": "0192f000-0000-7000-8000-000000000001"},
    "condition": {
        "match": "all",
        "rules": [
            {"source": "tag", "key": "vip", "op": "has"},
            {"source": "custom_field", "key": "score", "op": ">", "value": 10},
        ],
    },
    "smart_delay": {
        "mode": "duration",
        "duration": {"value": 30, "unit": "minutes"},
        "continue_window": {
            "enabled": True,
            "days": ["mon", "tue", "wed", "thu", "fri"],
            "from": "09:00",
            "to": "17:00",
            "use_contact_timezone": True,
        },
    },
    "randomizer": {"paths": [{"id": "a", "weight": 50}, {"id": "b", "weight": 50}], "sticky": True},
    "external_request": {
        "method": "POST",
        "url": "https://api.example.test/hooks/lead",
        "headers": [{"name": "X-Api-Key", "value": "{{api_key}}"}],
        "body": {"name": "{{first_name}}"},
        "timeout_s": 5,
        "response_mappings": [{"json_path": "$.id", "target_type": "variable", "target": "external_id"}],
        "fallback_handle_on_error": True,
    },
    "data_collection": {
        "question": "What email should we use?",
        "reply_type": "email",
        "target": {"type": "system_field", "key": "email"},
        "retry": {"max": 2, "invalid_text": "That does not look like an email address."},
        "timeout": {"enabled": True, "delay": 1, "unit": "days"},
    },
    "send_sms": {"text": "Your code is {{code}}."},
    "send_email": {"subject": "Welcome", "html_body": "<p>Hi {{first_name}}</p>"},
    "note": {"text": "Reviewed by ops on the 3rd."},
}

# Where every handle under test points. An action node because it needs no
# channel, so it can never be the thing that produces a capability warning in a
# fixture that is supposed to be clean.
_SINK = {
    "id": "sink",
    "type": "action",
    "position": {"x": 480, "y": 0},
    "config": {"actions": [{"verb": "close_conversation"}]},
}


def node_fixture(node_type: str, node_id: str = "subject", x: int = 0, y: int = 0) -> dict[str, Any]:
    """A single node of ``node_type`` with a valid config."""
    return {
        "id": node_id,
        "type": node_type,
        "position": {"x": x, "y": y},
        "config": deepcopy(NODE_CONFIGS[node_type]),
    }


def graph_for(node_type: str) -> dict[str, Any]:
    """A complete, valid graph whose entry node is a ``node_type`` node.

    A ``note`` gets a send_message alongside it: a note takes no part in routing,
    so a graph made only of notes has no entry node at all (SPEC §9.1).
    """
    graph = empty_graph()
    subject = node_fixture(node_type)

    if node_type == "note":
        entry = node_fixture("send_message", node_id="entry")
        entry["config"] = {"blocks": [{"type": "text", "text": "Hello."}]}
        graph["nodes"] = [entry, deepcopy(subject)]
        return graph

    graph["nodes"] = [subject]
    handles = sorted(handles_for_node(NODE_TYPES[node_type], subject["config"]))
    if handles:
        graph["nodes"].append(deepcopy(_SINK))
        graph["edges"] = [
            {"id": f"e{index}", "source": subject["id"], "sourceHandle": handle, "target": _SINK["id"]}
            for index, handle in enumerate(handles, start=1)
        ]
    return graph


def valid_graphs() -> dict[str, dict[str, Any]]:
    """Every node type's valid graph, keyed by type."""
    return {node_type: graph_for(node_type) for node_type in NODE_TYPES}


def button_heavy_graph() -> dict[str, Any]:
    """A send_message leaning on buttons and quick replies.

    The acceptance criterion's input: valid everywhere, and on a channel without
    buttons it must produce *warnings* rather than errors.
    """
    graph = empty_graph()
    node = node_fixture("send_message", node_id="ask")
    node["config"] = {
        "blocks": [{"type": "text", "text": "Pick one:"}, {"type": "card", "title": "Our plans"}],
        "buttons": [
            {"id": "b1", "label": "Plans", "action": "url", "url": "https://example.test/plans"},
            {"id": "b2", "label": "Sales", "action": "postback"},
            {"id": "b3", "label": "Support", "action": "postback"},
            {"id": "b4", "label": "Something else", "action": "postback"},
        ],
        "quick_replies": [{"id": "q1", "label": "Not now"}],
    }
    graph["nodes"] = [node]
    return graph
