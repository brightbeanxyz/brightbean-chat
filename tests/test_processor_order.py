"""The inbound seam's dispatch order, as the running app assembles it.

A project-level test rather than an app-level one because the invariant spans
three apps and no single one of them owns it — and because
``apps/channels/tests/conftest.py`` clears the seam for every test in the app
that defines it, so the real registry cannot be observed from there.

The assertion looks trivial and is not. Getting the order wrong is silent: the
preview stage would start a draft before persistence had recorded the tester's
consent (every first send refused with ``no_opt_in``) and before L4-A's routing
tail, which would then hand the very ``/start`` that opened the preview to the
execution it had just created.
"""

from apps.channels.ingest import DEFAULT_ORDER, LATE_ORDER, registered_processors


def test_persistence_then_routing_then_preview() -> None:
    order = registered_processors()
    assert order.index("persistence") < order.index("routing")
    assert order.index("preview") > order.index("routing")


def test_the_late_band_is_genuinely_after_the_default_one() -> None:
    """The preview's position must not depend on INSTALLED_APPS order.

    ``apps.channels`` — which registers the preview — is listed *before*
    ``apps.messaging``, which registers persistence and routing. So if bands
    were ignored and dispatch followed registration order, the preview would run
    first. This is the assertion that says the band is what puts it last.
    """
    assert LATE_ORDER > DEFAULT_ORDER
