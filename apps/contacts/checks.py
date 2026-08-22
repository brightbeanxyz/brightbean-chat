"""Boot-time checks on the condition engine's allowlists.

The engine's safety rests on two tables — :data:`~apps.contacts.conditions.SYSTEM_FIELDS`
and the per-source operator declarations — and both are hand-written. A typo in
either is a *developer* mistake rather than an attacker's, but the failure modes
are poor: an allowlist entry naming a relation would let a filter traverse one,
and a declared operator with no compiler branch would raise ``KeyError`` at
evaluation time in production rather than in review.

Running at boot rather than only in a test follows ``apps/common/checks.py``:
the invariant then holds even for a branch whose author forgot the test.
"""

from typing import Any

from django.core.checks import CheckMessage, Error, Tags, register


@register(Tags.models)
def check_condition_allowlists(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """``contacts.E001``/``E002`` — the allowlists must describe reality."""
    from apps.contacts.conditions import ALL_OPS, OP_LABELS, SYSTEM_FIELDS
    from apps.contacts.models import Contact

    errors: list[CheckMessage] = []

    concrete = {
        field.name: field
        for field in Contact._meta.get_fields()
        if getattr(field, "concrete", False) and not field.is_relation
    }
    for key, spec in SYSTEM_FIELDS.items():
        if spec.column not in concrete:
            errors.append(
                Error(
                    f"system_field {key!r} maps to {spec.column!r}, which is not a concrete "
                    f"non-relational field on Contact.",
                    hint=(
                        "The condition engine turns this name into a query kwarg. A relation "
                        "or a missing column would let a filter traverse out of the contact "
                        "row. Fix SYSTEM_FIELDS in apps/contacts/conditions.py."
                    ),
                    id="contacts.E001",
                )
            )
            continue
        if concrete[spec.column].null != spec.nullable:
            errors.append(
                Error(
                    f"system_field {key!r} declares nullable={spec.nullable}, but "
                    f"Contact.{spec.column} has null={concrete[spec.column].null}.",
                    hint=(
                        "A predicate on a nullable column is wrapped so `~q` stays an exact "
                        "complement; getting this wrong makes a contact fall out of both "
                        "halves of an operator pair. See _definite() in conditions.py."
                    ),
                    id="contacts.E001",
                )
            )

    missing_labels = sorted(ALL_OPS - set(OP_LABELS))
    if missing_labels:
        errors.append(
            Error(
                f"These operators have no human label: {', '.join(missing_labels)}.",
                hint=(
                    "OP_LABELS is published in CONDITION_SCHEMA's x-brightbean block and is "
                    "what the flow builder renders, so an unlabelled operator reaches a user "
                    "as a raw token."
                ),
                id="contacts.E002",
            )
        )
    return errors
