"""Flow export and import — SPEC §21 phase 3, "template flows".

Portable flow JSON so an automation can be shared between installations. The
surface is small on purpose; import from this package rather than from the
modules under it:

    from apps.flows.portability import export_document, parse_and_validate, plan_import

Four modules sit behind it, and the division is the point.
:mod:`~apps.flows.portability.refs` owns the one definition of "what in a flow
is workspace-local", and both directions walk with it, so the exporter cannot
scrub a reference the importer does not know how to resolve.
:mod:`~apps.flows.portability.envelope` owns the document format and the caps
that guard it. :mod:`~apps.flows.portability.export` scrubs and translates;
:mod:`~apps.flows.portability.imports` validates, asks, and — only then —
writes.

Two properties are worth stating because everything else leans on them:

* **The exported document is a valid flow graph.** Synthetic references are
  UUIDs, so a scrubbed graph still passes ``apps.flows.schema.validate_graph``
  and a scrubbed trigger config still passes
  ``apps.flows.triggers.validation.validate_config`` — which is what lets an
  untrusted file be validated in full before anything touches the ORM.
* **The serialisation is canonical.** Sorted keys, no timestamps, references
  numbered in walk order. Export, import into a clean workspace, export again,
  and the two files are byte-identical — which is how the round-trip guarantee
  is asserted rather than described.
"""

from apps.flows.portability.envelope import (
    APP_NAME,
    FORMAT_VERSION,
    MAX_DOCUMENT_BYTES,
    MAX_FLOWS,
    REQUIREMENT_KINDS,
    parse,
    serialize,
    validate_envelope,
)
from apps.flows.portability.export import export_document, export_filename, flow_closure
from apps.flows.portability.imports import (
    ACTION_BLANK,
    ACTION_CREATE,
    ACTION_KEEP,
    ACTION_MAP,
    ACTION_SKIP,
    CREATABLE_KINDS,
    TRIGGER_KIND,
    ImportNotReadyError,
    ImportPlan,
    Requirement,
    Resolution,
    TriggerChoice,
    apply_import,
    default_mapping,
    outbound_requests,
    parse_and_validate,
    plan_import,
    requirements_for,
    trigger_choices,
)

__all__ = [
    "ACTION_BLANK",
    "ACTION_CREATE",
    "ACTION_KEEP",
    "ACTION_MAP",
    "ACTION_SKIP",
    "APP_NAME",
    "CREATABLE_KINDS",
    "FORMAT_VERSION",
    "MAX_DOCUMENT_BYTES",
    "MAX_FLOWS",
    "REQUIREMENT_KINDS",
    "TRIGGER_KIND",
    "ImportNotReadyError",
    "ImportPlan",
    "Requirement",
    "Resolution",
    "TriggerChoice",
    "apply_import",
    "default_mapping",
    "export_document",
    "export_filename",
    "flow_closure",
    "outbound_requests",
    "parse",
    "parse_and_validate",
    "plan_import",
    "requirements_for",
    "serialize",
    "trigger_choices",
    "validate_envelope",
]
