"""Model invariants: tenancy, uniqueness, the derived workspace, one typed value."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.common.scoping import UnscopedQueryError
from apps.contacts.errors import WorkspaceMismatchError
from apps.contacts.models import (
    Contact,
    ContactTag,
    CustomField,
    CustomFieldType,
    CustomFieldValue,
    Segment,
    Tag,
    exactly_one_value_populated,
)

TENANT_MODELS = (Contact, Tag, ContactTag, CustomField, CustomFieldValue, Segment)


@pytest.mark.django_db
class TestTenancyIsEnforced:
    @pytest.mark.parametrize("model", TENANT_MODELS, ids=lambda m: m.__name__)
    def test_an_unscoped_query_refuses_to_run(self, model):
        with pytest.raises(UnscopedQueryError):
            list(model.objects.all())

    @pytest.mark.parametrize("model", TENANT_MODELS, ids=lambda m: m.__name__)
    def test_the_default_manager_is_the_plain_one(self, model):
        """common.E004's invariant, asserted per model rather than only globally.

        Django's admin, cascades and reverse related access all go through
        _default_manager, and the enforcing manager raises there.
        """
        assert model._meta.default_manager.name == "all_objects"

    def test_a_scoped_query_sees_only_its_own_workspace(self, tenancy, other_tenancy):
        mine = Tag.objects.create(workspace=tenancy.workspace, name="mine")
        Tag.objects.create(workspace=other_tenancy.workspace, name="theirs")

        assert [t.pk for t in Tag.objects.for_workspace(tenancy.workspace)] == [mine.pk]


@pytest.mark.django_db
class TestNamesAreUniquePerWorkspace:
    def test_the_same_tag_name_in_two_workspaces_is_fine(self, tenancy, other_tenancy):
        Tag.objects.create(workspace=tenancy.workspace, name="VIP")
        Tag.objects.create(workspace=other_tenancy.workspace, name="VIP")

        assert Tag.objects.for_workspace(other_tenancy.workspace).count() == 1

    @pytest.mark.parametrize("second", ["VIP", "vip", "ViP"])
    def test_a_case_variant_of_an_existing_tag_is_refused(self, workspace, second):
        """Stricter than SPEC's (workspace, name): "VIP" and "vip" as two tags is
        a data-quality bug visible in the first tag picker."""
        Tag.objects.create(workspace=workspace, name="VIP")

        with pytest.raises(IntegrityError):
            Tag.objects.create(workspace=workspace, name=second)

    def test_a_case_variant_custom_field_is_refused(self, workspace):
        CustomField.objects.create(workspace=workspace, name="Plan", type=CustomFieldType.TEXT)

        with pytest.raises(IntegrityError):
            CustomField.objects.create(workspace=workspace, name="plan", type=CustomFieldType.NUMBER)

    def test_the_clash_is_reported_as_a_validation_error_by_full_clean(self, workspace):
        """Expression constraints are checked by validate_constraints(), which
        full_clean() calls — so a form reports the clash instead of a 500."""
        Tag.objects.create(workspace=workspace, name="VIP")

        with pytest.raises(ValidationError):
            Tag(workspace=workspace, name="vip").full_clean()


@pytest.mark.django_db
class TestTheDerivedWorkspace:
    def test_a_join_row_takes_the_contacts_workspace_even_when_handed_another(
        self, tenancy, other_tenancy, contact, tag
    ):
        link = ContactTag(workspace=other_tenancy.workspace, contact=contact, tag=tag)
        link.save()

        link.refresh_from_db()
        assert link.workspace_id == tenancy.workspace.pk

    def test_a_tag_from_another_workspace_is_refused(self, other_tenancy, contact):
        theirs = Tag.objects.create(workspace=other_tenancy.workspace, name="theirs")

        with pytest.raises(WorkspaceMismatchError):
            ContactTag(contact=contact, tag=theirs).save()

    def test_a_field_from_another_workspace_is_refused(self, other_tenancy, contact):
        theirs = CustomField.objects.create(workspace=other_tenancy.workspace, name="Plan", type=CustomFieldType.TEXT)

        with pytest.raises(WorkspaceMismatchError):
            CustomFieldValue(contact=contact, field=theirs, value_text="x").save()

    def test_update_fields_still_carries_the_derived_workspace(self, contact, custom_field):
        row = CustomFieldValue(contact=contact, field=custom_field, value_text="a")
        row.save()

        row.value_text = "b"
        row.save(update_fields=["value_text"])

        row.refresh_from_db()
        assert row.value_text == "b"
        assert row.workspace_id == contact.workspace_id


@pytest.mark.django_db
class TestExactlyOneTypedValue:
    def test_two_populated_columns_are_refused_by_the_database(self, contact, custom_field):
        with pytest.raises(IntegrityError), transaction.atomic():
            CustomFieldValue(contact=contact, field=custom_field, value_text="a", value_number=1).save()

    def test_a_row_with_no_value_at_all_is_refused(self, contact, custom_field):
        """Exactly one, not at most one: clearing a value deletes the row."""
        with pytest.raises(IntegrityError), transaction.atomic():
            CustomFieldValue(contact=contact, field=custom_field).save()

    @pytest.mark.parametrize(
        ("column", "value"),
        [("value_text", ""), ("value_bool", False), ("value_number", 0)],
    )
    def test_a_falsy_but_present_value_is_accepted(self, tenancy, contact, column, value):
        field = CustomField.objects.create(
            workspace=tenancy.workspace,
            name=column,
            type={"value_text": CustomFieldType.TEXT, "value_bool": CustomFieldType.BOOLEAN}.get(
                column, CustomFieldType.NUMBER
            ),
        )
        CustomFieldValue(contact=contact, field=field, **{column: value}).save()

        assert CustomFieldValue.objects.for_workspace(tenancy.workspace).count() == 1

    def test_the_constraint_covers_every_declared_value_column(self):
        """Generated from ALL_VALUE_COLUMNS, so a sixth field type cannot land
        with a stale constraint."""
        rendered = str(exactly_one_value_populated())

        for column in ("value_text", "value_number", "value_date", "value_datetime", "value_bool"):
            assert column in rendered

    def test_clean_names_the_column_a_mismatched_type_should_have_used(self, contact, custom_field):
        row = CustomFieldValue(contact=contact, field=custom_field, value_number=1)

        with pytest.raises(ValidationError, match="value_text"):
            row.clean()


@pytest.mark.django_db
class TestDisplay:
    def test_a_contact_with_no_name_still_renders_something(self, workspace):
        bare = Contact.objects.create(workspace=workspace)

        assert str(bare).startswith("Contact ")

    def test_a_contact_falls_back_to_email_then_phone(self, workspace):
        by_email = Contact.objects.create(workspace=workspace, email="a@b.test")
        by_phone = Contact.objects.create(workspace=workspace, phone="+15551234567")

        assert str(by_email) == "a@b.test"
        assert str(by_phone) == "+15551234567"


@pytest.mark.django_db
class TestTheBootTimeAllowlistChecks:
    """contacts.E001/E002 — the invariant holds even on a branch whose author
    forgot the test, which is the point of running it at boot."""

    def test_the_allowlists_pass_as_shipped(self):
        from apps.contacts.checks import check_condition_allowlists

        assert check_condition_allowlists() == []

    def test_a_column_that_is_not_on_contact_is_reported(self, monkeypatch):
        from apps.contacts import conditions
        from apps.contacts.checks import check_condition_allowlists

        broken = dict(conditions.SYSTEM_FIELDS)
        broken["oops"] = conditions.SystemField("workspace__organization__name", conditions.TYPE_TEXT, "Oops")
        monkeypatch.setattr(conditions, "SYSTEM_FIELDS", broken)

        assert [error.id for error in check_condition_allowlists()] == ["contacts.E001"]

    def test_a_wrong_nullability_declaration_is_reported(self, monkeypatch):
        from apps.contacts import conditions
        from apps.contacts.checks import check_condition_allowlists

        broken = dict(conditions.SYSTEM_FIELDS)
        broken["email"] = conditions.SystemField("email", conditions.TYPE_TEXT, "Email", nullable=True)
        monkeypatch.setattr(conditions, "SYSTEM_FIELDS", broken)

        assert [error.id for error in check_condition_allowlists()] == ["contacts.E001"]

    def test_an_unlabelled_operator_is_reported(self, monkeypatch):
        from apps.contacts import conditions
        from apps.contacts.checks import check_condition_allowlists

        monkeypatch.setattr(conditions, "OP_LABELS", {})

        assert [error.id for error in check_condition_allowlists()] == ["contacts.E002"]


@pytest.mark.django_db
class TestTheReadOnlyTagsRelation:
    """``Contact.tags`` is for reading. The through table is written only by
    ``services``, which emits the contract-7 events."""

    def test_add_is_refused(self, contact, tag):
        with pytest.raises(RuntimeError, match="read-only"):
            contact.tags.add(tag)

    def test_remove_is_refused(self, contact, tag):
        """The one that used to succeed: `.remove()` only DELETEs, so it dropped
        the link with no contact.tag_removed emitted and nothing raised."""
        from apps.contacts import services

        services.add_tag(contact, tag)

        # Django wraps remove() in atomic(savepoint=False), so the refusal marks
        # the enclosing transaction broken — correct for a programming error, and
        # a 500 in a request. The extra atomic() here gives it a savepoint to
        # roll back to so the assertion below can still run.
        with pytest.raises(RuntimeError, match="read-only"), transaction.atomic():
            contact.tags.remove(tag)

        assert contact.contact_tags.count() == 1

    def test_clear_is_refused(self, contact, tag):
        from apps.contacts import services

        services.add_tag(contact, tag)

        with pytest.raises(RuntimeError, match="read-only"), transaction.atomic():
            contact.tags.clear()

        assert contact.contact_tags.count() == 1

    def test_reading_still_works(self, contact, tag):
        from apps.contacts import services

        services.add_tag(contact, tag)

        assert [t.pk for t in contact.tags.all()] == [tag.pk]


@pytest.mark.django_db
class TestEmptyUpdateFieldsStaysANoOp:
    def test_save_with_an_empty_update_fields_writes_nothing(self, contact, custom_field):
        """Django returns before touching the database for a falsy
        update_fields; widening it to {"workspace"} would defeat that."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        row = CustomFieldValue(contact=contact, field=custom_field, value_text="a")
        row.save()

        with CaptureQueriesContext(connection) as captured:
            row.save(update_fields=[])

        assert captured.captured_queries == []

    def test_a_non_empty_update_fields_still_carries_the_derived_workspace(self, contact, custom_field):
        row = CustomFieldValue(contact=contact, field=custom_field, value_text="a")
        row.save()
        row.workspace_id = None
        row.value_text = "b"

        row.save(update_fields=["value_text"])

        row.refresh_from_db()
        assert row.value_text == "b"
        assert row.workspace_id == contact.workspace_id
