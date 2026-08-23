"""Turning model rows into the documented response shapes.

Explicit functions rather than ``ModelSchema``: what a public API returns is a
contract, and a contract that grows a field the day someone adds a column is not
one. Every key in every response below was written on purpose, and adding one is
a visible diff.

These return plain dicts. Ninja still validates them against the schemas in
:mod:`apps.api.schemas`, so a serializer that drifts from its schema fails
loudly rather than shipping a half-populated object.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

__all__ = [
    "contact_payload",
    "custom_field_payload",
    "field_value_payload",
    "flow_payload",
    "message_payload",
    "tag_payload",
]


def tag_payload(tag: Any) -> dict[str, Any]:
    return {"id": tag.pk, "name": tag.name}


def contact_payload(contact: Any) -> dict[str, Any]:
    """One contact.

    ``tags`` reads the m2m through the contact, which is scoped by construction
    — the relation cannot reach another workspace's rows — so this is one of the
    few places a manager other than ``.for_workspace()`` is correct.
    """
    return {
        "id": contact.pk,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "email": contact.email,
        "phone": contact.phone,
        "locale": contact.locale,
        "timezone": contact.timezone,
        "status": contact.status,
        "tags": [tag_payload(tag) for tag in contact.tags.all()],
        "last_interaction_at": contact.last_interaction_at,
        "created_at": contact.created_at,
        "updated_at": contact.updated_at,
    }


def custom_field_payload(field: Any) -> dict[str, Any]:
    return {"id": field.pk, "name": field.name, "type": field.type}


def field_value_payload(field: Any, value: Any) -> dict[str, Any]:
    """One custom field value, typed the way it was sent.

    ``Decimal`` is unwrapped to a JSON number rather than serialised as a
    string. A caller that PUTs ``42`` and then GETs ``"42"`` has been handed an
    asymmetric contract, and asymmetry is what integrations break on.
    ``coerce_value`` caps a stored number at 14 digits with at most 6 decimal
    places, which round-trips through a double exactly, so nothing is lost on
    the way out.
    """
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    return {"field_id": field.pk, "name": field.name, "type": field.type, "value": value}


def flow_payload(flow: Any) -> dict[str, Any]:
    return {
        "id": flow.pk,
        "name": flow.name,
        "status": flow.status,
        "folder": flow.folder,
        "created_at": flow.created_at,
        "updated_at": flow.updated_at,
    }


def message_payload(message: Any) -> dict[str, Any]:
    """One message row, as the send endpoint reports it.

    ``error`` carries a machine-readable code from ``apps.messaging.codes`` when
    the send failed for an operational reason. A *compliance* refusal never
    reaches here — the router turns those into a 422 with the reason in
    ``detail`` — so this field is about the provider, not about consent.
    """
    return {
        "id": message.pk,
        "conversation_id": message.conversation_id,
        "contact_id": message.conversation.contact_id,
        "status": message.status,
        "source": message.source,
        "error": message.error or "",
        "created_at": message.created_at,
    }
