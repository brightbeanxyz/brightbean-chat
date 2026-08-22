"""The registry, and the DEBUG-raise / production-log policy."""

import pytest
from django.test import override_settings

from apps.notifications import events
from apps.notifications.events import (
    REGISTRY,
    NotificationCopyError,
    NotificationEvent,
    get_event,
    register_event,
    registered_choices,
)

#: The vocabulary later layers code against. The issue body lists the first
#: seven; the #7 trigger in docs/agent-prompts/layer-2.md adds the eighth.
REQUIRED_EVENT_TYPES = {
    "flow_loop_cap_hit",
    "flow_execution_failed",
    "channel_needs_reauth",
    "outbound_webhook_disabled",
    "inbox_reminder",
    "member_mentioned",
    "broadcast_finished",
    "whatsapp_template_reviewed",
}


@pytest.fixture
def isolated_registry():
    """Restore the registry, so a test that registers cannot leak into another."""
    snapshot = dict(REGISTRY)
    yield REGISTRY
    REGISTRY.clear()
    REGISTRY.update(snapshot)


class TestTheShippedVocabulary:
    def test_every_event_type_the_issue_names_is_registered(self):
        """This set is the contract Layers 3 to 6 code against."""
        assert set(REGISTRY) >= REQUIRED_EVENT_TYPES

    def test_every_event_renders_from_a_representative_context(self):
        context = {
            "flow_name": "Welcome",
            "contact_name": "Ada",
            "node_label": "Send message",
            "error": "boom",
            "channel_name": "Support bot",
            "platform_label": "Telegram",
            "url": "https://example.test/hook",
            "failure_count": 100,
            "note": "follow up",
            "actor_name": "Grace",
            "message": "take a look",
            "broadcast_name": "Launch",
            "sent": 10,
            "failed": 1,
            "skipped": 2,
            "template_name": "order_update",
            "status": "approved",
            "reason": "",
        }
        for event in REGISTRY.values():
            title, _ = events.render(event, context)

            assert title, f"{event.key} rendered an empty title"
            assert "{" not in title, f"{event.key} left an unfilled placeholder: {title!r}"

    def test_choices_are_label_sorted_for_the_filter_dropdown(self):
        labels = [label for _, label in registered_choices()]

        assert labels == sorted(labels)

    def test_burst_prone_events_do_not_email(self):
        """flow_execution_failed fires once per execution, so one broken flow in
        a busy workspace would be a mail storm."""
        assert REGISTRY["flow_execution_failed"].emails_by_default is False
        assert REGISTRY["broadcast_finished"].emails_by_default is False
        assert REGISTRY["inbox_reminder"].emails_by_default is False

    def test_operator_alerts_do_email(self):
        assert REGISTRY["flow_loop_cap_hit"].emails_by_default is True
        assert REGISTRY["channel_needs_reauth"].emails_by_default is True
        assert REGISTRY["outbound_webhook_disabled"].emails_by_default is True


class TestUnknownEventType:
    def test_it_logs_and_returns_none_in_production(self, caplog):
        """config/settings/test.py forces DEBUG=False, so this is the default
        path — a mis-typed event type must not take down the webhook that
        triggered it."""
        with caplog.at_level("ERROR", logger="apps.notifications.events"):
            assert get_event("no_such_event") is None

        assert "no_such_event" in caplog.text

    @override_settings(DEBUG=True)
    def test_it_raises_in_debug(self):
        with pytest.raises(NotificationCopyError, match="no_such_event"):
            get_event("no_such_event")

    @override_settings(DEBUG=True)
    def test_the_error_is_a_lookup_error_not_a_key_error(self):
        """An `except KeyError` up the stack must not swallow it."""
        with pytest.raises(LookupError):
            get_event("no_such_event")
        try:
            get_event("no_such_event")
        except LookupError as exc:
            assert not isinstance(exc, KeyError)


class TestMissingContext:
    def test_a_missing_required_key_logs_in_production(self, caplog):
        event = REGISTRY["flow_loop_cap_hit"]
        with caplog.at_level("ERROR", logger="apps.notifications.events"):
            title, _ = events.render(event, {})

        assert "flow_name" in caplog.text
        # Degraded, not exploded: the notification still reaches the person.
        assert "hit the loop cap" in title

    @override_settings(DEBUG=True)
    def test_a_missing_required_key_raises_in_debug(self):
        with pytest.raises(NotificationCopyError, match="flow_name"):
            events.render(REGISTRY["flow_loop_cap_hit"], {})

    def test_an_optional_key_degrades_to_a_blank(self):
        _, body = events.render(REGISTRY["flow_loop_cap_hit"], {"flow_name": "Welcome"})

        assert "contact_name" not in body
        assert "It ran 30 blocks" in body


class TestRegistration:
    def test_a_later_layer_can_add_its_own(self, isolated_registry):
        register_event(NotificationEvent(key="l4_thing", label="Thing", icon="bell", title="A thing"))

        assert get_event("l4_thing") is not None

    def test_a_conflicting_duplicate_is_refused(self, isolated_registry):
        """Two layers fighting over one key would show wrong copy in someone
        else's feature, so this raises in every environment."""
        register_event(NotificationEvent(key="dupe", label="Mine", icon="bell", title="Mine"))

        with pytest.raises(ValueError, match="already registered"):
            register_event(NotificationEvent(key="dupe", label="Theirs", icon="bell", title="Theirs"))

    def test_re_registering_the_identical_event_is_a_no_op(self, isolated_registry):
        """A module re-imported under the autoreloader must not crash the app."""
        event = NotificationEvent(key="same", label="Same", icon="bell", title="Same")
        register_event(event)
        register_event(event)

        assert REGISTRY["same"] == event


class TestCopyIsPlainText:
    def test_values_are_not_html_escaped_at_render_time(self):
        """The columns hold plain text and are escaped once, at display time by
        `{{ n.title }}`. A Django template here would render with autoescape on
        and store `Ben &amp; Jerry&#x27;s`, which then shows double-escaped."""
        title, _ = events.render(REGISTRY["flow_loop_cap_hit"], {"flow_name": "Ben & Jerry's"})

        assert "Ben & Jerry's" in title
        assert "&amp;" not in title

    def test_a_value_containing_braces_is_not_re_expanded(self, isolated_registry):
        """format_map substitutes once; a value is never itself a template."""
        event = register_event(NotificationEvent(key="braces", label="Braces", icon="bell", title="Hi {name}"))

        title, _ = events.render(event, {"name": "{flow_name}", "flow_name": "leaked"})

        assert title == "Hi {flow_name}"
