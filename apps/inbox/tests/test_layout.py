"""The inbox as the prototype draws it (HANDOFF §3, Inbox).

What the redesign changed that is more than paint: the list's compact
timestamps and "You:" previews, the status bar's window countdown, the day
dividers, and the contact panel that now carries the thread's labels, the last
automation run and the panel toggle.
"""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.inbox import services
from apps.inbox.rendering import day_label, short_age, time_left
from apps.messaging.models import ContactChannelIdentity, Conversation

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


class TestShortAge:
    @pytest.mark.parametrize(
        ("ago", "expected"),
        [
            (timedelta(seconds=20), "now"),
            (timedelta(minutes=14), "14m"),
            (timedelta(hours=3, minutes=5), "3h"),
            (timedelta(days=1), "Yesterday"),
            (timedelta(days=3), "Tue"),
            (timedelta(days=30), "9 Sep"),
            (timedelta(days=400), "4 Sep 2025"),
        ],
    )
    def test_it_says_it_the_way_a_chat_list_does(self, ago: timedelta, expected: str) -> None:
        with timezone.override(UTC):
            assert short_age(NOW - ago, now=NOW) == expected

    def test_hours_stop_at_midnight(self) -> None:
        """Twenty past midnight, a message from 23:00: two hours ago, but the
        useful word is the day."""
        now = datetime(2026, 10, 9, 0, 20, tzinfo=UTC)
        with timezone.override(UTC):
            assert short_age(now - timedelta(hours=1, minutes=20), now=now) == "Yesterday"

    def test_nothing_for_a_thread_with_no_messages(self) -> None:
        assert short_age(None) == ""


class TestTimeLeft:
    def test_hours_and_minutes(self) -> None:
        assert time_left(NOW + timedelta(hours=3, minutes=12, seconds=30), now=NOW) == "3h 12m"

    def test_minutes_only_under_an_hour(self) -> None:
        assert time_left(NOW + timedelta(minutes=45, seconds=10), now=NOW) == "45m"

    def test_a_closed_window_has_nothing_left(self) -> None:
        assert time_left(NOW - timedelta(minutes=1), now=NOW) == ""
        assert time_left(None) == ""


class TestDayLabel:
    def test_today_yesterday_then_dates(self) -> None:
        with timezone.override(UTC):
            assert day_label(NOW - timedelta(hours=2), now=NOW) == "Today"
            assert day_label(NOW - timedelta(days=1), now=NOW) == "Yesterday"
            assert day_label(NOW - timedelta(days=3), now=NOW) == "Tue 6 Oct"
            assert day_label(NOW - timedelta(days=400), now=NOW) == "4 Sep 2025"


@pytest.mark.django_db
class TestTheList:
    def test_a_teammates_last_word_reads_as_yours(self, agent_client: Any, url_for: Any, outbound: Any) -> None:
        outbound("Sent the invoice again.")

        body = agent_client.get(url_for("rows")).content.decode()

        assert "You: Sent the invoice again." in body

    def test_a_contacts_last_word_is_theirs(self, agent_client: Any, url_for: Any, inbound: Any) -> None:
        inbound("How much is the large size?")

        body = agent_client.get(url_for("rows")).content.decode()

        assert "How much is the large size?" in body
        assert "You: How much" not in body

    def test_the_row_says_how_long_ago_compactly(self, agent_client: Any, url_for: Any, inbound: Any) -> None:
        inbound("hello")

        body = agent_client.get(url_for("rows")).content.decode()

        assert '<span class="ib-row-time">now</span>' in body
        assert " ago" not in body


@pytest.mark.django_db
class TestTheThread:
    def test_the_status_bar_counts_down_the_window(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: ContactChannelIdentity
    ) -> None:
        identity.window_expires_at = timezone.now() + timedelta(hours=3, minutes=12, seconds=30)
        identity.save(update_fields=["window_expires_at"])

        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "Window open for 3h 12m" in body

    def test_a_lapsed_window_is_never_called_open(self) -> None:
        """Allowed after the window closed means allowed with a message tag —
        the bar used to print "Window open until" a time already past."""
        from django.template.loader import render_to_string

        body = render_to_string(
            "inbox/_thread_body.html",
            {
                "compliance": {
                    "allowed": True,
                    "tag": "HUMAN_AGENT",
                    "window_left": "",
                    "identity": {"window_expires_at": timezone.now() - timedelta(hours=2)},
                    "reason": "Sent with a message tag.",
                },
                "is_paused": False,
                "can_reply": False,
                "rendered": [],
            },
        )

        assert "Window open" not in body
        assert "Window closed, replies go out with a message tag" in body

    def test_automation_can_be_paused_when_the_channel_blocks_a_reply(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: ContactChannelIdentity
    ) -> None:
        """A flow may go on writing with a template where an agent cannot, so
        taking over must not depend on the agent being allowed to reply."""
        identity.opted_out_at = timezone.now()
        identity.save(update_fields=["opted_out_at"])

        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert "ib-banner-blocked" in body
        assert ">Pause automation</button>" in body
        assert "ib-banner-open" not in body

    def test_messages_sit_under_a_day_divider(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any, inbound: Any
    ) -> None:
        inbound("hello")

        body = agent_client.get(url_for("messages", conversation_id=conversation.pk)).content.decode()

        assert '<li class="ib-day" role="separator">Today</li>' in body

    def test_the_header_offers_the_panel_toggle(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        header = agent_client.get(url_for("header", conversation_id=conversation.pk)).content.decode()

        assert 'aria-controls="inbox-sidebar"' in header
        assert "Contact details" in header


@pytest.mark.django_db
class TestThePanel:
    def test_the_threads_labels_live_in_the_panel(
        self, tenancy: Any, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        label = services.create_label(tenancy.workspace, name="Refunds", color="#3B82F6")
        services.create_label(tenancy.workspace, name="Billing", color="#22C55E")
        services.apply_label(conversation, label, by=None)

        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert "Refunds" in panel
        assert 'name="label" value=' in panel, "the + Add menu offers the labels not yet on the thread"
        assert "Billing" in panel

    def test_it_reports_the_opt_in_of_this_conversations_address(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: ContactChannelIdentity
    ) -> None:
        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert "Subscribed" in panel

    def test_the_panel_renders_without_javascript(
        self, agent_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """Opened and closed by class, not hidden by x-cloak until Alpine runs."""
        page = agent_client.get(url_for("thread", conversation_id=conversation.pk)).content.decode()
        aside = page[page.index('<aside id="inbox-sidebar"') :]
        aside = aside[: aside.index(">")]

        assert "x-cloak" not in aside
        assert "x-show" not in aside

    def test_nothing_run_yet_is_said(self, agent_client: Any, url_for: Any, conversation: Conversation) -> None:
        panel = agent_client.get(url_for("sidebar", conversation_id=conversation.pk)).content.decode()

        assert "Nothing has run for this contact yet." in panel
