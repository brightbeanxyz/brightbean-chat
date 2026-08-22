"""Where SPEC §16's preview stage sits on the inbound seam.

This lives in ``apps/flows`` because ``FlowsConfig.ready()`` is what registers
it, and because ``apps/channels/tests/conftest.py`` clears the seam for every
test in that app — so the app that owns the registration is the only place the
real registry can be observed.

The assertion looks trivial and is not. The stage's position is decided by
``INSTALLED_APPS`` order, which nothing else in the codebase treats as
load-bearing, and getting it wrong is silent: the preview would start a draft
before persistence had recorded the tester's consent (every first send refused
with ``no_opt_in``) and before L4-A's routing tail, which would then hand the
very ``/start`` that opened the preview to the execution it had just created.
"""

from apps.channels.ingest import registered_processors


def test_preview_runs_after_persistence_and_routing() -> None:
    order = registered_processors()
    assert "preview" in order, "apps.flows.apps.FlowsConfig.ready() should have registered it"
    assert order.index("preview") > order.index("persistence")
    assert order.index("preview") > order.index("routing")
