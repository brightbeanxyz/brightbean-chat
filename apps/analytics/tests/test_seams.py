"""The two late-resolving seams, and what happens when analytics is absent.

``apps.messaging.analytics`` and ``apps.flows.analytics`` are the only doors into
this app from below. Both mirror ``apps.flows.messaging`` — resolved late,
no-ops when the app is not installed, and never allowed to cost a send.

A deployment without ``apps.analytics`` is not hypothetical: SPEC §2 lists the
app packages and nothing forces a self-hoster to keep all of them, and the same
"degrade rather than crash" rule already governs how the flow engine reaches the
messaging facade.
"""

from typing import Any
from uuid import uuid4

import pytest
from django.urls import reverse

from apps.flows import analytics as flows_seam
from apps.messaging import analytics as messaging_seam
from apps.messaging.models import MessageStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def uninstalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer "not installed" for ``apps.analytics`` and nothing else."""
    from django.apps import apps as django_apps

    real = django_apps.is_installed

    def fake(label: str) -> bool:
        return False if label == "apps.analytics" else real(label)

    monkeypatch.setattr(django_apps, "is_installed", fake)


class TestUrlConf:
    """A deployment that drops the app must still boot.

    The seams below let *code* degrade, but ``config/urls.py`` is the one place
    that reaches into this app eagerly: ``include()`` imports the module it names
    while the URLConf is being built, and this app's view modules import its
    models. Unguarded, "analytics is not installed" became "the process does not
    start", which is the opposite of what every other seam here promises.
    """

    def test_the_guard_does_not_import_the_module_it_is_guarding(self) -> None:
        """The trap that made the first version of the guard useless.

        Python evaluates arguments before the call, so a helper taking a built
        ``path("", include("apps.x.urls"))`` runs the import on the way in and
        raises before the guard is ever consulted — a guard that reads correctly
        and does nothing. Passing the module *path* is what defers it, and a
        module that does not exist at all is the cheapest way to prove it stayed
        deferred.
        """
        from config.urls import _if_installed

        assert _if_installed("apps.no_such_app", ("nowhere/", "apps.no_such_app.urls")) == []

    def test_the_whole_url_conf_builds_with_the_app_removed(self) -> None:
        """The real thing, not a stand-in: rebuild ``config.urls`` for real with
        ``apps.analytics`` out of ``INSTALLED_APPS``, and check that the routes
        it owns are simply absent rather than fatal."""
        import importlib

        from django.conf import settings
        from django.test import override_settings
        from django.urls import clear_url_caches

        import config.urls

        without = [app for app in settings.INSTALLED_APPS if app != "apps.analytics"]
        try:
            with override_settings(INSTALLED_APPS=without):
                rebuilt = importlib.reload(config.urls)
                clear_url_caches()
                names = {getattr(entry, "namespace", None) for entry in rebuilt.urlpatterns}
                assert "analytics" not in names
        finally:
            # The module is process-global; a reload left in place would leave
            # every later test resolving a URLConf built for a different app set.
            importlib.reload(config.urls)
            clear_url_caches()

        assert reverse("analytics:overview", kwargs={"workspace_id": uuid4()})
        assert reverse("click_redirect", kwargs={"token": "t"})


class TestMessagingSeam:
    def test_it_is_available_when_the_app_is(self) -> None:
        assert messaging_seam.available() is True

    def test_recording_is_a_no_op_without_the_app(self, uninstalled: None) -> None:
        assert messaging_seam.available() is False
        messaging_seam.record_status(object(), previous=MessageStatus.QUEUED, current=MessageStatus.SENT)

    def test_a_failing_counter_never_reaches_the_caller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A counter is reporting; a message is the product. An exception here
        would turn a delivered message into a failed one."""
        from apps.analytics import counters

        def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("counters are unwell")

        monkeypatch.setattr(counters, "record_message_status", explode)

        messaging_seam.record_status(object(), previous=MessageStatus.QUEUED, current=MessageStatus.SENT)


class TestFlowsSeam:
    def test_it_is_available_when_the_app_is(self) -> None:
        assert flows_seam.available() is True

    def test_instrument_returns_the_message_unchanged_without_the_app(self, uninstalled: None, execution: Any) -> None:
        from apps.channels.events import Button, OutboundMessage

        outbound = OutboundMessage(buttons=(Button(id="b1", label="Go", url="https://example.test/"),))

        assert (
            flows_seam.instrument(outbound, execution=execution, node_id="n1", platform="telegram", idempotency_key="k")
            is outbound
        )

    def test_stats_is_none_without_the_app(self, uninstalled: None, tenancy: Any, flow: Any) -> None:
        """Which is what ``flow_stats`` renders as ``available: false`` — the
        state the builder's overlay was written to distinguish from "no data"."""
        assert flows_seam.stats(tenancy.workspace, flow.pk) is None

    def test_a_failing_wrapper_returns_the_message_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch, execution: Any
    ) -> None:
        from apps.analytics import tracking
        from apps.channels.events import Button, OutboundMessage

        def explode(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("tracking is unwell")

        monkeypatch.setattr(tracking, "instrument", explode)
        outbound = OutboundMessage(buttons=(Button(id="b1", label="Go", url="https://example.test/"),))

        assert (
            flows_seam.instrument(outbound, execution=execution, node_id="n1", platform="telegram", idempotency_key="k")
            is outbound
        )
