"""The public API later layers call: idempotency, typed writes, merge."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.contacts import services
from apps.contacts.conditions import ConditionValidationError
from apps.contacts.errors import ContactsError, FieldTypeError, WorkspaceMismatchError
from apps.contacts.models import ContactStatus, CustomField, CustomFieldType, CustomFieldValue, Tag


@pytest.mark.django_db
class TestCreatingAContact:
    def test_text_is_trimmed_and_email_is_lowercased(self, workspace):
        contact = services.create_contact(workspace, first_name="  Ada  ", email="Ada@Example.TEST")

        assert contact.first_name == "Ada"
        assert contact.email == "ada@example.test"

    def test_an_unknown_source_is_refused(self, workspace):
        with pytest.raises(ContactsError):
            services.create_contact(workspace, source="telepathy")

    def test_it_does_not_deduplicate(self, workspace):
        """Identity dedup is issue #8's and import dedup is #13's; a third rule
        invented here is one both would have to unlearn."""
        first = services.create_contact(workspace, email="ada@example.test")
        second = services.create_contact(workspace, email="ada@example.test")

        assert first.pk != second.pk


@pytest.mark.django_db
class TestTags:
    def test_get_or_create_matches_case_insensitively(self, workspace):
        first, created = services.get_or_create_tag(workspace, "VIP")
        second, created_again = services.get_or_create_tag(workspace, "  vip ")

        assert created is True
        assert created_again is False
        assert first.pk == second.pk

    def test_a_blank_name_is_refused(self, workspace):
        with pytest.raises(ContactsError):
            services.get_or_create_tag(workspace, "   ")

    def test_adding_a_tag_twice_reports_the_second_as_a_no_op(self, contact, tag):
        assert services.add_tag(contact, tag) is True
        assert services.add_tag(contact, tag) is False
        assert contact.contact_tags.count() == 1

    def test_removing_a_tag_that_is_not_there_reports_false(self, contact, tag):
        assert services.remove_tag(contact, tag) is False

    def test_a_tag_from_another_workspace_is_refused(self, other_tenancy, contact):
        theirs = Tag.objects.create(workspace=other_tenancy.workspace, name="theirs")

        with pytest.raises(WorkspaceMismatchError):
            services.add_tag(contact, theirs)

    def test_renaming_onto_an_existing_name_is_refused(self, workspace, tag):
        services.get_or_create_tag(workspace, "Lead")

        with pytest.raises(ContactsError):
            services.rename_tag(tag, "lead")

    def test_deleting_a_tag_reports_how_many_links_went(self, contact, other_contact, tag):
        services.add_tag(contact, tag)
        services.add_tag(other_contact, tag)

        assert services.delete_tag(tag) == 2


@pytest.mark.django_db
class TestTypedFieldWrites:
    @pytest.mark.parametrize(
        ("field_type", "value", "column", "stored"),
        [
            (CustomFieldType.TEXT, "  gold  ", "value_text", "gold"),
            (CustomFieldType.NUMBER, 42, "value_number", Decimal("42")),
            (CustomFieldType.NUMBER, "1.5", "value_number", Decimal("1.5")),
            (CustomFieldType.DATE, "2026-08-21", "value_date", date(2026, 8, 21)),
            (CustomFieldType.DATE, date(2026, 8, 21), "value_date", date(2026, 8, 21)),
            (CustomFieldType.BOOLEAN, False, "value_bool", False),
        ],
    )
    def test_a_well_typed_value_lands_in_its_own_column(self, workspace, contact, field_type, value, column, stored):
        field = services.create_custom_field(workspace, name=f"f-{field_type}-{column}", field_type=field_type)

        row = services.set_field_value(contact, field, value)

        assert getattr(row, column) == stored
        assert row.value == stored

    @pytest.mark.parametrize(
        ("field_type", "value"),
        [
            (CustomFieldType.TEXT, 5),
            (CustomFieldType.TEXT, "x\x00y"),
            # isinstance(True, int) is True in Python, so this one is the reason
            # coerce_value excludes bool explicitly rather than by omission.
            (CustomFieldType.NUMBER, True),
            (CustomFieldType.NUMBER, "not a number"),
            (CustomFieldType.NUMBER, float("nan")),
            (CustomFieldType.NUMBER, float("inf")),
            (CustomFieldType.NUMBER, 1.12345678),
            (CustomFieldType.DATE, datetime(2026, 8, 21, tzinfo=UTC)),
            (CustomFieldType.DATE, "2026-02-30"),
            (CustomFieldType.DATETIME, datetime(2026, 8, 21)),
            (CustomFieldType.DATETIME, "not a moment"),
            (CustomFieldType.BOOLEAN, "true"),
            (CustomFieldType.BOOLEAN, 1),
        ],
    )
    def test_a_wrong_typed_value_is_refused(self, workspace, contact, field_type, value):
        field = services.create_custom_field(workspace, name=f"g-{field_type}-{value!r}"[:100], field_type=field_type)

        with pytest.raises(FieldTypeError):
            services.set_field_value(contact, field, value)

    def test_a_message_never_echoes_the_value(self, workspace, contact):
        """Custom-field values are contact PII heading for a log line and, from
        issue #25, an API error body."""
        field = services.create_custom_field(workspace, name="Plan", field_type=CustomFieldType.NUMBER)

        with pytest.raises(FieldTypeError) as exc:
            services.set_field_value(contact, field, "sensitive-secret-value")

        assert "sensitive-secret-value" not in str(exc.value)

    def test_rewriting_a_field_with_a_different_type_replaces_every_column(self, workspace, contact):
        text = services.create_custom_field(workspace, name="City", field_type=CustomFieldType.TEXT)
        services.set_field_value(contact, text, "Paris")

        row = services.set_field_value(contact, text, "Berlin")

        assert row.value_text == "Berlin"
        assert row.value_number is None

    def test_clearing_deletes_the_row_rather_than_nulling_it(self, contact, custom_field):
        services.set_field_value(contact, custom_field, "gold")

        assert services.clear_field_value(contact, custom_field) is True
        assert services.clear_field_value(contact, custom_field) is False
        assert CustomFieldValue.objects.for_workspace(contact.workspace_id).count() == 0

    def test_a_field_from_another_workspace_is_refused(self, other_tenancy, contact):
        theirs = CustomField.objects.create(workspace=other_tenancy.workspace, name="Plan", type=CustomFieldType.TEXT)

        with pytest.raises(WorkspaceMismatchError):
            services.set_field_value(contact, theirs, "x")

    def test_a_fields_type_cannot_be_changed(self, custom_field):
        """rename_custom_field is the only mutator, and it renames only — see its
        docstring for what a retype would orphan."""
        assert not hasattr(services, "retype_custom_field")

        services.rename_custom_field(custom_field, "Tier")
        custom_field.refresh_from_db()

        assert custom_field.name == "Tier"
        assert custom_field.type == CustomFieldType.TEXT


@pytest.mark.django_db
class TestMergingContacts:
    def test_tags_are_unioned_and_the_survivor_keeps_its_own_values(self, workspace, contact, other_contact):
        gold, _ = services.get_or_create_tag(workspace, "gold")
        lead, _ = services.get_or_create_tag(workspace, "lead")
        services.add_tag(contact, gold)
        services.add_tag(other_contact, lead)
        city = services.create_custom_field(workspace, name="City", field_type=CustomFieldType.TEXT)
        services.set_field_value(contact, city, "Paris")
        services.set_field_value(other_contact, city, "Berlin")

        merged = services.merge_contacts(primary=contact, duplicate=other_contact)

        assert {t.name for t in merged.tags.all()} == {"gold", "lead"}
        assert services.field_values_for(merged)[city.pk] == "Paris"

    def test_a_blank_scalar_is_filled_from_the_duplicate(self, workspace, contact):
        contact.phone = ""
        contact.save(update_fields=["phone"])
        duplicate = services.create_contact(workspace, phone="+15551234567", first_name="Bob")

        merged = services.merge_contacts(primary=contact, duplicate=duplicate)

        assert merged.phone == "+15551234567"
        assert merged.first_name == "Ada"

    def test_the_later_interaction_wins(self, workspace, contact):
        now = timezone.now()
        contact.last_interaction_at = now - timedelta(days=2)
        contact.save(update_fields=["last_interaction_at"])
        duplicate = services.create_contact(workspace, last_interaction_at=now)

        merged = services.merge_contacts(primary=contact, duplicate=duplicate)

        assert merged.last_interaction_at == now

    def test_the_duplicate_becomes_a_tombstone_rather_than_disappearing(self, contact, other_contact):
        """SPEC §5 put `deleted` in the enum for this; issue #29 owns hard delete,
        and issue #8 will need the row in order to re-point identities."""
        services.merge_contacts(primary=contact, duplicate=other_contact)
        other_contact.refresh_from_db()

        assert other_contact.status == ContactStatus.DELETED

    def test_merging_across_workspaces_is_refused(self, other_tenancy, contact):
        theirs = services.create_contact(other_tenancy.workspace, first_name="Eve")

        with pytest.raises(WorkspaceMismatchError):
            services.merge_contacts(primary=contact, duplicate=theirs)

    def test_merging_a_tombstone_again_is_refused(self, contact, other_contact):
        services.merge_contacts(primary=contact, duplicate=other_contact)

        with pytest.raises(ContactsError):
            services.merge_contacts(primary=contact, duplicate=other_contact)

    def test_a_contact_cannot_be_merged_into_itself(self, contact):
        with pytest.raises(ContactsError):
            services.merge_contacts(primary=contact, duplicate=contact)


@pytest.mark.django_db
class TestSegments:
    def test_a_filter_must_be_a_json_object(self, workspace):
        """conditions.validate() accepts a raw string and parses it, which is
        right for an API boundary — but storing that string would leave the
        column holding a JSON string rather than an object."""
        with pytest.raises(ContactsError):
            services.create_segment(workspace, name="Strings", filter_json='{"match": "all", "rules": []}')

    @pytest.mark.parametrize("document", [[], "all", 42, None, True])
    def test_a_non_object_document_is_refused(self, workspace, document):
        with pytest.raises(ContactsError):
            services.create_segment(workspace, name="Odd", filter_json=document)

    def test_an_invalid_filter_is_refused_before_the_row_is_written(self, workspace):
        from apps.contacts.models import Segment

        with pytest.raises(ConditionValidationError):
            services.create_segment(workspace, name="Broken", filter_json={"match": "sideways", "rules": []})

        assert Segment.objects.for_workspace(workspace).count() == 0

    def test_updating_a_filter_validates_it_too(self, workspace):
        segment = services.create_segment(workspace, name="Fine", filter_json={"match": "all", "rules": []})

        with pytest.raises(ConditionValidationError):
            services.update_segment(segment, filter_json={"match": "all", "rules": [{"source": "nope"}]})

        segment.refresh_from_db()
        assert segment.filter_json == {"match": "all", "rules": []}

    def test_a_segment_cannot_be_updated_to_reference_itself(self, workspace):
        segment = services.create_segment(workspace, name="Selfie", filter_json={"match": "all", "rules": []})

        with pytest.raises(ConditionValidationError):
            services.update_segment(
                segment,
                filter_json={"match": "all", "rules": [{"source": "segment", "key": str(segment.pk), "op": "in"}]},
            )


@pytest.mark.django_db
class TestNamesAreRefusedNotTruncated:
    @pytest.mark.parametrize(
        ("call", "noun"),
        [
            (lambda ws, name: services.get_or_create_tag(ws, name), "tag"),
            (lambda ws, name: services.create_custom_field(ws, name=name, field_type="text"), "field"),
            (
                lambda ws, name: services.create_segment(ws, name=name, filter_json={"match": "all", "rules": []}),
                "segment",
            ),
        ],
        ids=["tag", "field", "segment"],
    )
    def test_an_over_long_name_is_refused(self, workspace, call, noun):
        """Truncating would show the user a name they did not type — and collapse
        two names that differ only past the limit into a duplicate clash."""
        with pytest.raises(ContactsError, match="at most"):
            call(workspace, "x" * 101)

    def test_two_names_differing_past_the_limit_no_longer_collide(self, workspace):
        first = "a" * 99 + "one"
        second = "a" * 99 + "two"

        with pytest.raises(ContactsError):
            services.get_or_create_tag(workspace, first)
        with pytest.raises(ContactsError):
            services.get_or_create_tag(workspace, second)

    def test_a_contact_scalar_is_still_truncated_rather_than_refused(self, workspace):
        """The opposite call for ingest: dropping an inbound contact because a
        platform sent a 300-character display name would lose the message."""
        contact = services.create_contact(workspace, first_name="x" * 300)

        assert len(contact.first_name) == 150


@pytest.mark.django_db
class TestDuplicateNamesAreRefusedNotIntegrityErrors:
    def test_creating_a_second_segment_with_the_same_name(self, workspace):
        services.create_segment(workspace, name="VIPs", filter_json={"match": "all", "rules": []})

        with pytest.raises(ContactsError, match="already exists"):
            services.create_segment(workspace, name="vips", filter_json={"match": "all", "rules": []})

    def test_renaming_a_segment_onto_an_existing_name(self, workspace):
        services.create_segment(workspace, name="VIPs", filter_json={"match": "all", "rules": []})
        other = services.create_segment(workspace, name="Leads", filter_json={"match": "all", "rules": []})

        with pytest.raises(ContactsError, match="already exists"):
            services.update_segment(other, name="vips")

    def test_a_segment_may_keep_its_own_name(self, workspace):
        segment = services.create_segment(workspace, name="VIPs", filter_json={"match": "all", "rules": []})

        services.update_segment(segment, name="VIPs")

        segment.refresh_from_db()
        assert segment.name == "VIPs"

    @pytest.mark.parametrize("noun", ["tag", "field", "segment"])
    def test_a_lost_race_answers_like_the_single_threaded_path(self, workspace, noun, monkeypatch):
        """Both requests pass the check-then-write probe; the loser must get the
        same readable refusal, not the IntegrityError that would 500 and poison
        the enclosing transaction."""
        from django.db import IntegrityError

        def racing_probe(*args, **kwargs):
            return None  # every probe reports the name is free

        monkeypatch.setattr(services, "_assert_name_is_free", racing_probe)

        makers = {
            "tag": lambda: Tag.objects.create(workspace=workspace, name="Clash"),
            "field": lambda: services.create_custom_field(workspace, name="Clash", field_type="text"),
            "segment": lambda: services.create_segment(
                workspace, name="Clash", filter_json={"match": "all", "rules": []}
            ),
        }
        makers[noun]()

        with pytest.raises((ContactsError, IntegrityError)) as exc:
            makers[noun]()

        if noun != "tag":  # the tag maker bypasses the service on purpose
            assert isinstance(exc.value, ContactsError)


@pytest.mark.django_db
class TestDeleteCountsAreAboutLiveContacts:
    def test_deleting_a_tag_counts_only_live_contacts(self, workspace, tag):
        live = services.create_contact(workspace, first_name="Live")
        gone = services.create_contact(workspace, first_name="Gone")
        services.add_tag(live, tag)
        services.add_tag(gone, tag)
        gone.status = ContactStatus.DELETED
        gone.save(update_fields=["status"])

        assert services.delete_tag(tag) == 1

    def test_deleting_a_field_counts_only_live_contacts(self, workspace, custom_field):
        live = services.create_contact(workspace, first_name="Live")
        gone = services.create_contact(workspace, first_name="Gone")
        services.set_field_value(live, custom_field, "a")
        services.set_field_value(gone, custom_field, "b")
        gone.status = ContactStatus.DELETED
        gone.save(update_fields=["status"])

        assert services.delete_custom_field(custom_field) == 1


@pytest.mark.django_db
class TestMergeDoesNotReProbeTagsItAlreadyFiltered:
    def test_merging_reads_the_link_table_twice_however_many_tags_there_are(self, workspace):
        """The claim, stated directly: the two set-building SELECTs, and no
        per-tag probe re-establishing what those sets already say.

        Asserted on the shape rather than a total, because each insert also
        takes a savepoint — a number that would change for reasons unrelated to
        this fix."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        primary = services.create_contact(workspace, first_name="Primary")
        duplicate = services.create_contact(workspace, first_name="Duplicate")
        for index in range(5):
            tag, _ = services.get_or_create_tag(workspace, f"t{index}")
            services.add_tag(duplicate, tag)

        with CaptureQueriesContext(connection) as captured:
            services.merge_contacts(primary=primary, duplicate=duplicate)

        selects = [
            q["sql"]
            for q in captured.captured_queries
            if q["sql"].lstrip().upper().startswith("SELECT") and "contacts_contact_tag" in q["sql"]
        ]

        assert len(selects) == 2, selects
        assert primary.tags.count() == 5
