"""Event-log retention (SPEC §5)."""

from datetime import timedelta
from typing import Any

import pytest
from django.core.management import call_command
from django.test.utils import override_settings
from django.utils import timezone

from apps.channels.housekeeping import prune_webhook_event_log
from apps.channels.models import WebhookEventLog

pytestmark = pytest.mark.django_db


def make_events(connection: Any, count: int, *, age_days: int) -> None:
    received = timezone.now() - timedelta(days=age_days)
    for index in range(count):
        WebhookEventLog.objects.create(
            connection=connection,
            platform=connection.platform,
            provider_event_id=f"{age_days}-{index}",
            received_at=received,
        )


class TestPrune:
    def test_old_rows_go_and_recent_rows_stay(self, connection: Any) -> None:
        make_events(connection, 3, age_days=40)
        make_events(connection, 2, age_days=5)

        assert prune_webhook_event_log() == 3
        assert sorted(WebhookEventLog.objects.values_list("provider_event_id", flat=True)) == ["5-0", "5-1"]

    def test_the_window_is_configurable(self, connection: Any) -> None:
        make_events(connection, 2, age_days=10)
        assert prune_webhook_event_log(older_than_days=3) == 2

    @override_settings(WEBHOOK_EVENT_LOG_RETENTION_DAYS=1)
    def test_the_setting_is_the_default(self, connection: Any) -> None:
        make_events(connection, 1, age_days=2)
        assert prune_webhook_event_log() == 1

    def test_nothing_to_do_is_not_an_error(self, connection: Any) -> None:
        assert prune_webhook_event_log() == 0

    def test_a_backlog_larger_than_one_batch_is_fully_cleared(self, connection: Any, monkeypatch: Any) -> None:
        """The loop exists so a first run does not take one enormous lock."""
        monkeypatch.setattr("apps.channels.housekeeping.PRUNE_BATCH_SIZE", 2)
        make_events(connection, 7, age_days=40)
        assert prune_webhook_event_log() == 7
        assert WebhookEventLog.objects.count() == 0


class TestManagementCommand:
    def test_it_runs_without_the_task_queue(self, connection: Any) -> None:
        """#5 is a parallel sibling, so this is the only guaranteed way to prune."""
        make_events(connection, 2, age_days=40)
        call_command("prune_webhook_events")
        assert WebhookEventLog.objects.count() == 0

    def test_the_window_can_be_given_on_the_command_line(self, connection: Any) -> None:
        make_events(connection, 2, age_days=10)
        call_command("prune_webhook_events", "--older-than-days", "3")
        assert WebhookEventLog.objects.count() == 0
