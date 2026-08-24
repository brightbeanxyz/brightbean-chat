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

import pytest

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
