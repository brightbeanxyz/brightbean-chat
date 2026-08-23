"""Triggers and inbound routing — issue #11 (L4-A).

Six modules, split by what they know about:

* :mod:`~apps.flows.triggers.types` — SPEC §10's vocabulary as data. Imports
  nothing from this app, because :mod:`apps.flows.models` imports *it*.
* :mod:`~apps.flows.triggers.schema` — per-type ``config_json`` schemas.
* :mod:`~apps.flows.triggers.matching` — the per-type matcher registry.
* :mod:`~apps.flows.triggers.routing` — the ordered hook registry, ROADMAP
  contract 6. This is the layer's public deliverable: L5-D registers STOP/HELP
  at ``hard_optout`` and L6-C registers inbox rules at ``post_persist``.
* :mod:`~apps.flows.triggers.pipeline` — the processor registered into the
  contract-6 seam, plus the five stages themselves.
* :mod:`~apps.flows.triggers.budget` — SPEC §7.1's inline-vs-enqueue decision.

**This module deliberately imports nothing.** ``apps.flows.models`` needs
``TriggerType`` at import time, so a package ``__init__`` that pulled in
``matching`` (which needs the models) would be a cycle. Registration happens in
``FlowsConfig.ready()``, which names the modules with side effects explicitly —
the same pattern the node and queue-handler registries already use.
"""
