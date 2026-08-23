"""Writing a custom field from a node — resolution and type coercion.

Two node runtimes write custom fields and they arrive at it from opposite
directions. The action node's ``set_field`` verb (SPEC §11.2) always has a
**string**: its schema says so, and it has to, because the value may be a
``{{placeholder}}`` whose type is unknown until render time. The External
Request node (SPEC §11.7) always has whatever JSON the far end sent — a number,
a boolean, a string, sometimes an object.

:func:`apps.contacts.services.coerce_value` is the one type gate in the product
and neither of those is quite what it accepts, so the adaptation lives here,
once, rather than as a private helper in each node. It stays deliberately thin:
the actual write is still ``set_field_value``, and this module adds no second
path into ``custom_field_value``.
"""

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from apps.contacts.models import CustomField, CustomFieldType

if TYPE_CHECKING:
    from apps.flows.engine.context import NodeContext

__all__ = ["FALSE_WORDS", "TRUE_WORDS", "custom_field_by_name", "typed_for"]

logger = logging.getLogger(__name__)

#: Strings a boolean custom field accepts from a flow.
#:
#: ``coerce_value`` deliberately refuses a string for a boolean field — "``True``
#: would quietly store as ``1``" is its worry, and it is right for an API. A
#: flow's value comes out of a text box or off the wire, so the conversion
#: happens here, with an allowlist rather than Python truthiness, under which
#: the string ``"false"`` is true.
TRUE_WORDS = frozenset({"true", "yes", "y", "1", "on"})
FALSE_WORDS = frozenset({"false", "no", "n", "0", "off"})


def custom_field_by_name(ctx: "NodeContext", name: Any, *, node_id: str = "") -> CustomField | None:
    """A custom field of *this workspace* by name, case-insensitively.

    ``None`` with a warning when the workspace has no such field: a flow naming
    a field somebody deleted should log and carry on, not end the run.

    Scoped through ``for_workspace`` rather than looked up by id, which is what
    keeps a hand-edited graph from addressing another tenant's field
    (SECURITY-BASELINE §1). Raises ``ValueError`` on a blank name — that is a
    graph the validator should never have passed, and the callers turn it into
    their own logged skip.
    """
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned:
        raise ValueError("A custom field target needs a field name.")
    field = CustomField.objects.for_workspace(ctx.workspace_id).filter(name__iexact=cleaned).first()
    if field is None:
        logger.warning(
            "Execution %s: node %s names custom field %r, which this workspace does not have.",
            ctx.execution.pk,
            node_id or ctx.node_id,
            cleaned,
        )
    return field


def typed_for(field: CustomField, value: Any) -> Any:
    """Turn a flow's value into something ``coerce_value`` accepts.

    Only two shapes need help, and both are cases ``coerce_value`` is right to
    refuse from an API and wrong to refuse from a flow:

    * a **boolean field** given text — mapped through the word lists above, and
      a word in neither raises ``ValueError`` rather than guessing.
    * a **text field** given a JSON number — an ``$.order.id`` of ``4172`` is
      the commonest External Request mapping there is, and refusing it because
      the far end sent a number rather than a string would be pedantry with no
      safety behind it. ``bool`` is excluded on purpose: ``True`` in a text field
      should read as the author's own words, not as Python's.

    Everything else is handed through untouched, so dates, datetimes, numbers
    and their string forms keep going through the one type gate rather than
    around it.
    """
    if field.type == CustomFieldType.BOOLEAN:
        if isinstance(value, bool):
            return value
        word = value.strip().casefold() if isinstance(value, str) else ""
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError(f"{field.name} holds true or false; {value!r} is neither.")

    if field.type == CustomFieldType.TEXT and isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return str(value)

    return value
