"""ROADMAP contract 7: fixed names, fixed payloads, and no event for a no-op.

The dotted names are a wire format — issue #25 stores them in
``outbound_webhook.events`` — so a rename is a silent unsubscribe for every
webhook already configured. That is what the first test here exists to prevent.
"""

from contextlib import contextmanager

import pytest

from apps.contacts import services
from apps.contacts.events import (
    CONTACT_EVENT_NAMES,
    EVENT_CATALOG,
    EVENT_CONTACT_CREATED,
    EVENT_CONTACT_FIELD_CHANGED,
    EVENT_CONTACT_TAG_ADDED,
    EVENT_CONTACT_TAG_REMOVED,
    emit,
)
from apps.contacts.models import CustomFieldType


@contextmanager
def capture(*events: str):
    """Collect payloads for the named events for the duration of the block."""
    seen: list[dict] = []

    def receiver(sender, **kwargs):
        seen.append(kwargs)

    for name in events:
        EVENT_CATALOG[name].connect(receiver, weak=False)
    try:
        yield seen
    finally:
        for name in events:
            EVENT_CATALOG[name].disconnect(receiver)


class TestTheCatalogIsAWireFormat:
    def test_the_dotted_names_are_exactly_the_four_the_contract_fixes(self):
        assert set(EVENT_CATALOG) == {
            "contact.created",
            "contact.tag_added",
            "contact.tag_removed",
            "contact.field_changed",
        }

    def test_the_name_tuple_matches_the_catalog(self):
        assert set(CONTACT_EVENT_NAMES) == set(EVENT_CATALOG)

    def test_an_unknown_event_name_is_a_crash_not_a_silent_no_op(self):
        with pytest.raises(KeyError):
            emit("contact.exploded", workspace_id=None, contact_id=None)


@pytest.mark.django_db
class TestEachServiceCallFiresItsEvent:
    def test_creating_a_contact(self, workspace):
        with capture(EVENT_CONTACT_CREATED) as seen:
            contact = services.create_contact(workspace, first_name="Ada", source="api")

        assert len(seen) == 1
        assert seen[0]["event"] == "contact.created"
        assert seen[0]["workspace_id"] == workspace.pk
        assert seen[0]["contact_id"] == contact.pk
        assert seen[0]["source"] == "api"

    def test_adding_and_removing_a_tag(self, contact, tag):
        with capture(EVENT_CONTACT_TAG_ADDED, EVENT_CONTACT_TAG_REMOVED) as seen:
            services.add_tag(contact, tag)
            services.remove_tag(contact, tag)

        assert [item["event"] for item in seen] == ["contact.tag_added", "contact.tag_removed"]
        assert all(item["tag_id"] == tag.pk and item["contact_id"] == contact.pk for item in seen)

    def test_setting_and_clearing_a_field(self, contact, custom_field):
        with capture(EVENT_CONTACT_FIELD_CHANGED) as seen:
            services.set_field_value(contact, custom_field, "gold")
            services.clear_field_value(contact, custom_field)

        assert [item["cleared"] for item in seen] == [False, True]
        assert all(item["field_id"] == custom_field.pk for item in seen)


@pytest.mark.django_db
class TestAnUnchangedWriteIsNotAnEvent:
    def test_re_adding_a_tag_fires_nothing(self, contact, tag):
        services.add_tag(contact, tag)

        with capture(EVENT_CONTACT_TAG_ADDED) as seen:
            services.add_tag(contact, tag)

        assert seen == []

    def test_removing_a_tag_that_was_never_there_fires_nothing(self, contact, tag):
        with capture(EVENT_CONTACT_TAG_REMOVED) as seen:
            services.remove_tag(contact, tag)

        assert seen == []

    def test_rewriting_the_same_field_value_fires_nothing(self, contact, custom_field):
        services.set_field_value(contact, custom_field, "gold")

        with capture(EVENT_CONTACT_FIELD_CHANGED) as seen:
            services.set_field_value(contact, custom_field, "gold")

        assert seen == []


@pytest.mark.django_db
class TestWhereEventsDeliberatelyDoNotFire:
    def test_deleting_a_tag_does_not_fan_out_per_contact_removals(self, workspace, contact, other_contact, tag):
        """One administrative click must not become N rule evaluations and N
        webhook deliveries. Asserted so the decision cannot change by accident —
        see services.delete_tag."""
        services.add_tag(contact, tag)
        services.add_tag(other_contact, tag)

        with capture(EVENT_CONTACT_TAG_REMOVED) as seen:
            services.delete_tag(tag)

        assert seen == []

    def test_deleting_a_custom_field_does_not_fan_out_per_contact_changes(self, workspace, contact):
        field = services.create_custom_field(workspace, name="City", field_type=CustomFieldType.TEXT)
        services.set_field_value(contact, field, "Paris")

        with capture(EVENT_CONTACT_FIELD_CHANGED) as seen:
            services.delete_custom_field(field)

        assert seen == []


@pytest.mark.django_db
class TestEventsAreAtomicWithTheirCause:
    def test_a_rolled_back_write_takes_its_event_with_it(self, workspace):
        """Sent inside the transaction, not through on_commit: a subscriber's
        job is to enqueue a scheduled_action in the same database, so the side
        effect and its cause roll back together."""
        from django.db import transaction

        with capture(EVENT_CONTACT_CREATED) as seen, pytest.raises(RuntimeError), transaction.atomic():
            services.create_contact(workspace, first_name="Ada")
            raise RuntimeError("boom")

        # The event was sent, and so was the insert; both are gone now.
        assert len(seen) == 1
        from apps.contacts.models import Contact

        assert Contact.objects.for_workspace(workspace).count() == 0
