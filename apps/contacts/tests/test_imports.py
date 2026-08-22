"""CSV import: the batched handler, the dry run, dedupe, and the two promises.

The two promises are what most of this file is about.

**"50k rows in the background, no web-request timeouts."** The wizard's requests
only ever store a mapping and enqueue; the work is a queue action that processes
a bounded slice and re-schedules itself. So the tests drive the handler directly
and assert that a file longer than one batch takes several, that each one resumes
from ``next_offset``, and that a crash mid-batch does not double-create.

**"Imported contacts are not channel-reachable."** No path here creates a
``ContactChannelIdentity``, and the test asserts the count is unchanged rather
than asserting the absence of a call — a future refactor could reach the model a
different way, and the count would still catch it.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.contacts import imports, services
from apps.contacts.models import (
    Contact,
    ContactImport,
    CustomFieldType,
    ImportDedupe,
    ImportStatus,
    Tag,
)
from apps.messaging.models import ContactChannelIdentity
from apps.queueing.models import ActionStatus, ScheduledAction


def make_run(workspace, text: str, **kwargs) -> ContactImport:
    """A ContactImport with ``text`` stored as its file."""
    run = ContactImport(workspace=workspace, original_filename="people.csv", consent_ack=True, **kwargs)
    run.save()
    run.file.save(f"{run.pk}.csv", SimpleUploadedFile("x.csv", text.encode("utf-8")), save=True)
    return run


def drain(run: ContactImport, mode: str, *, limit: int = 200) -> int:
    """Run the handler repeatedly until the run finishes. Returns the batch count.

    Stands in for the worker: it claims the pending rows this run scheduled and
    calls the handler with their payloads, which is exactly what
    ``apps.queueing.worker`` does minus the locking it does not need here (the
    rows deliberately name no contact).
    """
    batches = 0
    while batches < limit:
        action = (
            ScheduledAction.objects.for_workspace(run.workspace_id)
            .filter(type=imports.ACTION_TYPE, status=ActionStatus.PENDING)
            .order_by("created_at")
            .first()
        )
        if action is None:
            return batches
        action.status = ActionStatus.DONE
        action.save(update_fields=["status"])
        imports.handle_contact_import(action.payload, action)
        batches += 1
        run.refresh_from_db()
    raise AssertionError("import did not finish")


SIMPLE = "first,last,email\nAda,Lovelace,ada@example.test\nGrace,Hopper,grace@example.test\n"
MAPPING = {"0": "system:first_name", "1": "system:last_name", "2": "system:email"}


@pytest.mark.django_db
class TestReadingTheFile:
    def test_a_utf8_bom_does_not_become_part_of_the_first_column_name(self, tenancy):
        """Excel writes one. Read as plain utf-8 it corrupts exactly one column's
        heading, so the mapping silently loses that column and nothing else."""
        run = make_run(tenancy.workspace, "﻿first,email\nAda,ada@example.test\n")

        assert imports.read_header(run) == ["first", "email"]

    def test_a_semicolon_delimited_file_is_recognised(self, tenancy):
        run = make_run(tenancy.workspace, "first;email\nAda;ada@example.test\n")

        assert imports.read_header(run) == ["first", "email"]

    def test_a_file_that_is_not_utf8_degrades_to_replacement_rather_than_failing(self, tenancy):
        run = ContactImport(workspace=tenancy.workspace, consent_ack=True)
        run.save()
        run.file.save(f"{run.pk}.csv", SimpleUploadedFile("x.csv", b"first,email\n\xe9ric,e@x.test\n"), save=True)

        assert imports.read_header(run) == ["first", "email"]
        assert imports.preview(run)[0][1] == "e@x.test"

    def test_an_empty_file_is_refused(self, tenancy):
        run = make_run(tenancy.workspace, "")

        with pytest.raises(imports.UnusableImportError):
            imports.read_header(run)

    def test_a_file_with_too_many_columns_is_refused(self, tenancy):
        run = make_run(tenancy.workspace, ",".join(f"c{i}" for i in range(imports.MAX_COLUMNS + 1)))

        with pytest.raises(imports.UnusableImportError):
            imports.read_header(run)


@pytest.mark.django_db
class TestTheMapping:
    def test_a_mapping_naming_a_deleted_custom_field_is_refused(self, tenancy):
        field = services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        mapping = {"0": f"field:{field.pk}"}
        field.delete()

        with pytest.raises(imports.UnusableImportError):
            imports.resolve_mapping(tenancy.workspace, mapping, ["plan"])

    def test_a_custom_field_from_another_workspace_reads_as_deleted(self, tenancy, other_tenancy):
        """Scoped resolution, so a foreign id is absent rather than forbidden —
        the same no-existence-oracle rule every other id in this app follows."""
        theirs = services.create_custom_field(other_tenancy.workspace, name="Theirs", field_type=CustomFieldType.TEXT)

        with pytest.raises(imports.UnusableImportError):
            imports.resolve_mapping(tenancy.workspace, {"0": f"field:{theirs.pk}"}, ["x"])

    def test_a_system_target_outside_the_allowlist_is_refused(self, tenancy):
        """`status` and `workspace` are columns on Contact; the allowlist is what
        stops a hand-crafted mapping reaching them."""
        with pytest.raises(imports.UnusableImportError):
            imports.resolve_mapping(tenancy.workspace, {"0": "system:status"}, ["x"])

    def test_two_columns_mapped_to_the_same_field_are_refused(self, tenancy):
        with pytest.raises(imports.UnusableImportError):
            imports.resolve_mapping(tenancy.workspace, {"0": "system:email", "1": "system:email"}, ["a", "b"])

    def test_an_index_outside_the_file_is_ignored_rather_than_crashing(self, tenancy):
        resolved = imports.resolve_mapping(tenancy.workspace, {"0": "system:email", "9": "tags"}, ["email"])

        assert resolved.system == {0: "email"}
        assert resolved.tags_column is None

    def test_email_wins_over_phone_as_the_dedupe_key(self, tenancy):
        resolved = imports.resolve_mapping(
            tenancy.workspace, {"0": "system:phone", "1": "system:email"}, ["phone", "email"]
        )

        assert resolved.match_field == "email"


@pytest.mark.django_db
class TestTheDryRun:
    def test_it_writes_nothing(self, tenancy):
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING)
        imports.enqueue(run, mode=imports.MODE_DRY_RUN)

        drain(run, imports.MODE_DRY_RUN)

        assert run.status == ImportStatus.VALIDATED
        assert run.created_count == 2
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 0

    def test_it_catches_a_type_error_before_a_single_row_is_written(self, tenancy):
        """The point of the preview: "last Tuesday" in a date column is found
        while nothing has been written, not half way through the import."""
        field = services.create_custom_field(tenancy.workspace, name="Renews", field_type=CustomFieldType.DATE)
        run = make_run(
            tenancy.workspace,
            "email,renews\nada@example.test,2026-01-01\ngrace@example.test,last Tuesday\n",
            mapping={"0": "system:email", "1": f"field:{field.pk}"},
        )
        imports.enqueue(run, mode=imports.MODE_DRY_RUN)

        drain(run, imports.MODE_DRY_RUN)

        assert run.error_count == 1
        assert run.errors[0]["row"] == 2
        assert run.errors[0]["column"] == "Renews"
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 0

    def test_a_malformed_email_is_a_row_error_not_a_failed_run(self, tenancy):
        run = make_run(
            tenancy.workspace,
            "email\nada@example.test\nnot-an-email\n",
            mapping={"0": "system:email"},
        )
        imports.enqueue(run, mode=imports.MODE_DRY_RUN)

        drain(run, imports.MODE_DRY_RUN)

        assert run.status == ImportStatus.VALIDATED
        assert run.error_count == 1
        assert run.created_count == 1

    def test_it_learns_how_long_the_file_is(self, tenancy):
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING)
        imports.enqueue(run, mode=imports.MODE_DRY_RUN)

        drain(run, imports.MODE_DRY_RUN)

        assert run.total_rows == 2


@pytest.mark.django_db
class TestImporting:
    def test_it_creates_contacts_through_the_service_so_events_fire(self, tenancy):
        from apps.contacts.events import EVENT_CATALOG, EVENT_CONTACT_CREATED

        seen: list = []
        EVENT_CATALOG[EVENT_CONTACT_CREATED].connect(lambda **kw: seen.append(kw["source"]), weak=False)
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert run.status == ImportStatus.DONE
        assert run.created_count == 2
        assert seen == ["import", "import"]
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 2

    def test_no_channel_identity_is_ever_fabricated(self, tenancy):
        """The promise the consent copy makes. Asserted on the row count rather
        than on a call, so a future refactor that reached the model another way
        would still be caught."""
        run = make_run(
            tenancy.workspace,
            "email,phone\nada@example.test,+445550101\n",
            mapping={"0": "system:email", "1": "system:phone"},
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert Contact.objects.for_workspace(tenancy.workspace).count() == 1
        assert ContactChannelIdentity.objects.for_workspace(tenancy.workspace).count() == 0

    def test_a_tags_column_splits_on_either_separator_and_creates_missing_tags(self, tenancy):
        run = make_run(
            tenancy.workspace,
            "email,tags\nada@example.test,VIP; Beta\ngrace@example.test,VIP\n",
            mapping={"0": "system:email", "1": "tags"},
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert set(Tag.objects.for_workspace(tenancy.workspace).values_list("name", flat=True)) == {"VIP", "Beta"}
        ada = Contact.objects.for_workspace(tenancy.workspace).get(email="ada@example.test")
        assert set(ada.tags.values_list("name", flat=True)) == {"VIP", "Beta"}

    def test_a_tags_cell_naming_the_same_tag_twice_links_it_once(self, tenancy):
        run = make_run(
            tenancy.workspace,
            "email,tags\nada@example.test,VIP;vip\n",
            mapping={"0": "system:email", "1": "tags"},
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert Tag.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_empty_row_is_reported_rather_than_creating_a_blank_contact(self, tenancy):
        run = make_run(
            tenancy.workspace,
            "email\nada@example.test\n\n",
            mapping={"0": "system:email"},
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert Contact.objects.for_workspace(tenancy.workspace).count() == 1


@pytest.mark.django_db
class TestDedupe:
    FILE = "email,first\nada@example.test,Augusta\n"

    def _run(self, workspace, dedupe):
        run = make_run(
            workspace,
            self.FILE,
            mapping={"0": "system:email", "1": "system:first_name"},
            dedupe=dedupe,
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)
        drain(run, imports.MODE_IMPORT)
        return run

    def test_update_fills_the_existing_contact(self, tenancy):
        existing = services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")

        run = self._run(tenancy.workspace, ImportDedupe.UPDATE)

        assert run.updated_count == 1
        existing.refresh_from_db()
        assert existing.first_name == "Augusta"
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 1

    def test_create_makes_a_second_contact(self, tenancy):
        services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")

        run = self._run(tenancy.workspace, ImportDedupe.CREATE)

        assert run.created_count == 1
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 2

    def test_skip_leaves_everything_alone(self, tenancy):
        existing = services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")

        run = self._run(tenancy.workspace, ImportDedupe.SKIP)

        assert run.skipped_count == 1
        existing.refresh_from_db()
        assert existing.first_name == "Ada"

    def test_matching_is_case_insensitive_because_create_contact_lowercases(self, tenancy):
        services.create_contact(tenancy.workspace, first_name="Ada", email="ADA@example.test")

        run = self._run(tenancy.workspace, ImportDedupe.UPDATE)

        assert run.updated_count == 1

    def test_a_soft_deleted_contact_is_not_resurrected_by_an_import(self, tenancy):
        """Updating a tombstone would put somebody the operator removed back into
        a send path without anybody choosing that."""
        gone = services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")
        services.delete_contact(gone)

        run = self._run(tenancy.workspace, ImportDedupe.UPDATE)

        assert run.created_count == 1
        gone.refresh_from_db()
        assert gone.first_name == "Ada"

    def test_a_blank_cell_does_not_clear_a_stored_value(self, tenancy):
        """A partial export re-imported to fill in phone numbers must not wipe
        every name it left out."""
        existing = services.create_contact(
            tenancy.workspace, first_name="Ada", email="ada@example.test", phone="+445550101"
        )
        run = make_run(
            tenancy.workspace,
            "email,first,phone\nada@example.test,Augusta,\n",
            mapping={"0": "system:email", "1": "system:first_name", "2": "system:phone"},
            status=ImportStatus.VALIDATED,
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)
        drain(run, imports.MODE_IMPORT)

        existing.refresh_from_db()
        assert existing.first_name == "Augusta"
        assert existing.phone == "+445550101"


@pytest.mark.django_db
class TestBatching:
    def _big(self, workspace, rows: int, **kwargs):
        buffer = io.StringIO()
        buffer.write("email\n")
        for index in range(rows):
            buffer.write(f"person{index}@example.test\n")
        return make_run(workspace, buffer.getvalue(), mapping={"0": "system:email"}, **kwargs)

    def test_a_file_longer_than_one_batch_takes_several_and_finishes(self, tenancy, settings):
        settings.CONTACT_IMPORT_BATCH_ROWS = 10
        run = self._big(tenancy.workspace, 25, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        batches = drain(run, imports.MODE_IMPORT)

        assert batches == 3
        assert run.status == ImportStatus.DONE
        assert run.created_count == 25
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 25

    def test_a_batch_never_names_a_contact_so_it_cannot_hold_the_advisory_lock(self, tenancy):
        """apps.queueing.worker takes the contact lock when a row names one, and
        bulk work holding it for a whole batch would stall every flow for that
        contact behind the import (SPEC §9.6)."""
        run = self._big(tenancy.workspace, 3, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        rows = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=imports.ACTION_TYPE)

        assert rows.exists()
        assert all(row.contact_id is None for row in rows)

    def test_a_replayed_batch_does_not_double_create(self, tenancy, settings):
        """A crashed batch rolls back its rows *and* its counters, so the retry
        starts from the same next_offset. Replaying a batch that already
        committed is the other half: the offset guard makes it a no-op."""
        settings.CONTACT_IMPORT_BATCH_ROWS = 10
        run = self._big(tenancy.workspace, 15, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)
        first = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=imports.ACTION_TYPE)

        imports.handle_contact_import(first.payload, first)
        run.refresh_from_db()
        assert run.created_count == 10

        imports.handle_contact_import(first.payload, first)  # the duplicate delivery
        run.refresh_from_db()

        assert run.created_count == 10
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 10

    def test_the_row_cap_stops_the_run_and_says_so(self, tenancy, settings):
        settings.CONTACT_IMPORT_MAX_ROWS = 5
        settings.CONTACT_IMPORT_BATCH_ROWS = 10
        run = self._big(tenancy.workspace, 12, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert run.status == ImportStatus.DONE
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 5
        assert any("more than" in error["message"] for error in run.errors)

    def test_row_errors_are_capped_but_still_counted(self, tenancy, monkeypatch, settings):
        settings.CONTACT_IMPORT_BATCH_ROWS = 50
        monkeypatch.setattr(ContactImport, "MAX_REPORTED_ROW_ERRORS", 3)
        buffer = io.StringIO()
        buffer.write("email\n")
        for index in range(10):
            buffer.write(f"not-an-email-{index}\n")
        run = make_run(
            tenancy.workspace, buffer.getvalue(), mapping={"0": "system:email"}, status=ImportStatus.VALIDATED
        )
        imports.enqueue(run, mode=imports.MODE_IMPORT)

        drain(run, imports.MODE_IMPORT)

        assert run.error_count == 10
        assert len(run.errors) == 3
        assert run.errors_truncated is True
        assert run.hidden_error_count == 7


@pytest.mark.django_db
class TestRunFailures:
    def test_a_missing_file_fails_the_run_rather_than_burning_the_retry_ladder(self, tenancy):
        """Five attempts over six hours cannot make a deleted file reappear."""
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.VALIDATED)
        imports.enqueue(run, mode=imports.MODE_IMPORT)
        action = ScheduledAction.objects.for_workspace(tenancy.workspace).get(type=imports.ACTION_TYPE)
        run.file.delete(save=True)

        imports.handle_contact_import(action.payload, action)

        run.refresh_from_db()
        assert run.status == ImportStatus.FAILED

    def test_an_import_that_is_already_finished_ignores_a_stale_batch(self, tenancy):
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.DONE)
        action = imports.enqueue(run, mode=imports.MODE_IMPORT)

        imports.handle_contact_import(action.payload, action)

        assert Contact.objects.for_workspace(tenancy.workspace).count() == 0

    def test_an_action_naming_a_deleted_import_is_dropped_quietly(self, tenancy):
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING)
        action = imports.enqueue(run, mode=imports.MODE_DRY_RUN)
        run.delete()

        imports.handle_contact_import(action.payload, action)  # must not raise


@pytest.mark.django_db
class TestHousekeeping:
    def test_a_finished_run_loses_its_file_but_keeps_its_report(self, tenancy, settings):
        from datetime import timedelta

        from django.utils import timezone

        from apps.contacts.housekeeping import prune_import_files

        settings.CONTACT_IMPORT_FILE_RETENTION_DAYS = 30
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.DONE)
        run.error_count = 2
        run.errors = [{"row": 1, "column": "", "message": "bad"}]
        run.finished_at = timezone.now() - timedelta(days=31)
        run.save()

        prune_import_files()

        run.refresh_from_db()
        assert not run.file
        assert run.errors == [{"row": 1, "column": "", "message": "bad"}]

    def test_a_recent_run_keeps_its_file(self, tenancy):
        from django.utils import timezone

        from apps.contacts.housekeeping import prune_import_files

        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.DONE)
        run.finished_at = timezone.now()
        run.save(update_fields=["finished_at"])

        prune_import_files()

        run.refresh_from_db()
        assert run.file

    def test_the_job_is_registered_for_the_hourly_sweep(self):
        from apps.queueing.housekeeping import housekeeping_jobs

        assert "prune_contact_import_files" in housekeeping_jobs()


@pytest.mark.django_db
class TestTheWizard:
    def url(self, tenancy, suffix):
        return f"/w/{tenancy.workspace.id}/{suffix}"

    def test_an_upload_without_the_consent_box_is_refused(self, tenancy, client_for):
        """The box is the one thing about this feature that surprises people, and
        `consent_ack` records that the person importing was told."""
        response = client_for(tenancy.owner).post(
            self.url(tenancy, "contacts/import/upload/"),
            {"file": SimpleUploadedFile("x.csv", SIMPLE.encode())},
        )

        assert response.status_code == 204
        assert not ContactImport.objects.for_workspace(tenancy.workspace).exists()

    def test_an_upload_over_the_size_cap_is_refused(self, tenancy, client_for, settings):
        settings.CONTACT_IMPORT_MAX_BYTES = 10

        response = client_for(tenancy.owner).post(
            self.url(tenancy, "contacts/import/upload/"),
            {"file": SimpleUploadedFile("x.csv", SIMPLE.encode()), "consent_ack": "on"},
        )

        assert "too large" in response.headers["HX-Trigger"]
        assert not ContactImport.objects.for_workspace(tenancy.workspace).exists()

    def test_a_good_upload_stores_the_file_and_the_acknowledgement(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            self.url(tenancy, "contacts/import/upload/"),
            {"file": SimpleUploadedFile("people.csv", SIMPLE.encode()), "consent_ack": "on"},
        )

        assert response.status_code == 204
        run = ContactImport.objects.for_workspace(tenancy.workspace).get()
        assert run.consent_ack is True
        assert run.created_by == tenancy.owner
        assert run.original_filename == "people.csv"

    def test_the_uploaded_name_never_becomes_part_of_the_storage_path(self, tenancy, client_for):
        """`import_upload_to` builds the path from the workspace and the row's own
        id; the uploaded name is display text and decides nothing."""
        client_for(tenancy.owner).post(
            self.url(tenancy, "contacts/import/upload/"),
            {"file": SimpleUploadedFile("../../etc/passwd", SIMPLE.encode()), "consent_ack": "on"},
        )

        run = ContactImport.objects.for_workspace(tenancy.workspace).get()
        assert run.file.name == f"contact-imports/{tenancy.workspace.pk}/{run.pk}.csv"

    def test_an_unreadable_upload_is_rejected_and_leaves_no_row(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            self.url(tenancy, "contacts/import/upload/"),
            {"file": SimpleUploadedFile("x.csv", b""), "consent_ack": "on"},
        )

        assert "HX-Trigger" in response.headers
        assert not ContactImport.objects.for_workspace(tenancy.workspace).exists()

    def test_mapping_stores_the_choice_and_queues_the_dry_run(self, tenancy, client_for):
        run = make_run(tenancy.workspace, SIMPLE)

        client_for(tenancy.owner).post(
            self.url(tenancy, f"contacts/import/{run.pk}/mapping/"),
            {"column-0": "system:first_name", "column-2": "system:email", "dedupe": ImportDedupe.SKIP},
        )

        run.refresh_from_db()
        assert run.mapping == {"0": "system:first_name", "2": "system:email"}
        assert run.dedupe == ImportDedupe.SKIP
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=imports.ACTION_TYPE).exists()

    def test_an_unusable_mapping_is_a_toast_and_is_not_stored(self, tenancy, client_for):
        run = make_run(tenancy.workspace, SIMPLE)

        response = client_for(tenancy.owner).post(
            self.url(tenancy, f"contacts/import/{run.pk}/mapping/"), {"dedupe": ImportDedupe.UPDATE}
        )

        assert "error" in response.headers["HX-Trigger"]
        run.refresh_from_db()
        assert run.mapping == {}

    def test_importing_before_the_check_has_finished_is_refused(self, tenancy, client_for):
        """An operator who has not seen the row errors has not been told what this
        is about to do."""
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.UPLOADED)

        response = client_for(tenancy.owner).post(self.url(tenancy, f"contacts/import/{run.pk}/run/"))

        assert "Check the file first" in response.headers["HX-Trigger"]
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=imports.ACTION_TYPE).exists()

    def test_the_report_downloads_as_csv_with_the_truncation_note(self, tenancy, client_for):
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.DONE)
        run.errors = [{"row": 2, "column": "email", "message": "That is not an email address."}]
        run.error_count = 9
        run.save(update_fields=["errors", "error_count"])

        response = client_for(tenancy.owner).get(self.url(tenancy, f"contacts/import/{run.pk}/report/"))

        body = b"".join(response.streaming_content).decode()
        assert response["Content-Type"].startswith("text/csv")
        assert "not an email address" in body
        assert "8 further row error(s)" in body

    def test_another_workspaces_import_is_a_404(self, tenancy, other_tenancy, client_for):
        theirs = make_run(other_tenancy.workspace, SIMPLE, mapping=MAPPING)

        response = client_for(tenancy.owner).get(self.url(tenancy, f"contacts/import/{theirs.pk}/"))

        assert response.status_code == 404


@pytest.mark.django_db
class TestConcurrencyAndRepeats:
    def test_rechecking_after_fixing_the_mapping_actually_reruns(self, tenancy, client_for):
        """The reason ``enqueue`` carries no idempotency key. The obvious one —
        run/mode/offset — is already in the table from the first attempt, and
        ``schedule`` answers "already arranged" whatever that row's status, so a
        second check would silently do nothing."""
        run = make_run(tenancy.workspace, SIMPLE)
        client = client_for(tenancy.owner)
        base = f"/w/{tenancy.workspace.id}/contacts/import/{run.pk}/mapping/"

        client.post(base, {"column-2": "system:email", "dedupe": ImportDedupe.UPDATE})
        drain(run, imports.MODE_DRY_RUN)
        assert run.status == ImportStatus.VALIDATED

        client.post(base, {"column-0": "system:first_name", "column-2": "system:email", "dedupe": ImportDedupe.UPDATE})
        batches = drain(run, imports.MODE_DRY_RUN)

        assert batches == 1
        assert run.status == ImportStatus.VALIDATED

    def test_two_batch_zero_rows_do_not_both_write(self, tenancy):
        """A double-clicked "Import for real" queues two batch-zero actions. The
        row lock plus the offset guard means the second one finds itself stale."""
        run = make_run(tenancy.workspace, SIMPLE, mapping=MAPPING, status=ImportStatus.VALIDATED)
        first = imports.enqueue(run, mode=imports.MODE_IMPORT)
        second = imports.enqueue(run, mode=imports.MODE_IMPORT)

        assert first.pk != second.pk

        imports.handle_contact_import(first.payload, first)
        imports.handle_contact_import(second.payload, second)

        run.refresh_from_db()
        assert run.created_count == 2
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 2


@pytest.mark.django_db
class TestTagValidation:
    def test_an_over_long_tag_name_is_caught_by_the_dry_run(self, tenancy):
        """Not only by the import. A preview that missed it would promise a clean
        file and then report the failure after half of it had been written."""
        long_name = "x" * (imports.MAX_TAG_NAME_CHARS + 1)
        run = make_run(
            tenancy.workspace,
            f"email,tags\nada@example.test,{long_name}\n",
            mapping={"0": "system:email", "1": "tags"},
        )
        imports.enqueue(run, mode=imports.MODE_DRY_RUN)

        drain(run, imports.MODE_DRY_RUN)

        assert run.error_count == 1
        assert run.errors[0]["column"] == imports.TAGS_TARGET
        assert Tag.objects.for_workspace(tenancy.workspace).count() == 0
