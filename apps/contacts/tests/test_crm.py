"""The CRM surface issue #13 adds: gating, the list, the detail page, bulk actions.

The role matrix is the part worth reading first. SPEC §4's permission table has
nine keys and none of them means "may see contacts", yet the issue requires an
Agent to edit a contact's tags and fields and a Viewer to see the CRM read-only.
So reading is gated on **workspace membership** and writing on the two keys SPEC
§4 does define, and this module pins every cell of that grid — including the ones
that must answer 403, because a permission test that only asserts the happy path
proves nothing.

``tests/idor.py`` sweeps every URL kwarg automatically. What it cannot see is a
tenant id in a **query string** (``?segment=``, ``?filter=``) or a **POST body**
(the bulk endpoints' ``ids``), so those are tested here directly.
"""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.contacts import services
from apps.contacts.models import Contact, ContactStatus, CustomFieldType, Segment, Tag

ALL_ROLES = ("admin", "editor", "agent", "viewer")
#: Holders of ``edit_contact_fields`` — SPEC §4 gives it to agent and above.
EDITORS = ("admin", "editor", "agent")
#: Holders of ``manage_crm`` — editor and above.
MANAGERS = ("admin", "editor")


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/{suffix}"


def triggers(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])


@pytest.fixture
def crm(db, tenancy):
    """A contact, a tag and a custom field in the victim's workspace."""
    contact = services.create_contact(tenancy.workspace, first_name="Ada", last_name="Lovelace", email="ada@x.test")
    tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
    field = services.create_custom_field(tenancy.workspace, name="Plan", field_type=CustomFieldType.TEXT)
    return {"contact": contact, "tag": tag, "field": field}


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReadGating:
    """Reading the CRM is "is a member of this workspace"."""

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_every_role_including_viewer_can_open_the_list(self, tenancy, client_for, role):
        response = client_for(tenancy.user_for(role)).get(url(tenancy, "contacts/"))

        assert response.status_code == 200

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_every_role_including_viewer_can_open_a_contact(self, tenancy, client_for, crm, role):
        response = client_for(tenancy.user_for(role)).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))

        assert response.status_code == 200

    def test_a_member_of_another_organization_gets_404_not_403(self, tenancy, other_tenancy, client_for, crm):
        """404, never 403: a 403 would confirm the workspace id names something
        real, which over a UUID space is the only thing an attacker was missing."""
        response = client_for(other_tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))

        assert response.status_code == 404

    def test_an_anonymous_visitor_is_sent_to_login(self, tenancy, client):
        response = client.get(url(tenancy, "contacts/"))

        assert response.status_code == 302
        assert "/accounts/login/" in response.headers["Location"]


@pytest.mark.django_db
class TestWriteGating:
    @pytest.mark.parametrize("role", EDITORS)
    def test_agent_and_above_may_edit_a_contacts_fields(self, tenancy, client_for, crm, role):
        response = client_for(tenancy.user_for(role)).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/edit/"), {"first_name": "Augusta"}
        )

        assert response.status_code == 204
        crm["contact"].refresh_from_db()
        assert crm["contact"].first_name == "Augusta"

    def test_a_viewer_may_not(self, tenancy, client_for, crm):
        response = client_for(tenancy.user_for("viewer")).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/edit/"), {"first_name": "Augusta"}
        )

        assert response.status_code == 403
        crm["contact"].refresh_from_db()
        assert crm["contact"].first_name == "Ada"

    @pytest.mark.parametrize("role", EDITORS)
    def test_agent_and_above_may_attach_an_existing_tag(self, tenancy, client_for, crm, role):
        response = client_for(tenancy.user_for(role)).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/tags/add/"), {"tag_id": str(crm["tag"].pk)}
        )

        assert response.status_code == 204
        assert crm["contact"].tags.count() == 1

    def test_an_agent_may_not_mint_a_new_tag(self, tenancy, client_for, crm):
        """Attaching a tag is edit_contact_fields; *creating* one is tag CRUD,
        which SPEC §4 puts behind manage_crm — it changes the vocabulary every
        segment and flow will later pick from."""
        response = client_for(tenancy.user_for("agent")).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/tags/add/"), {"name": "Brand new"}
        )

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"
        assert not Tag.objects.for_workspace(tenancy.workspace).filter(name="Brand new").exists()

    def test_an_editor_may(self, tenancy, client_for, crm):
        response = client_for(tenancy.user_for("editor")).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/tags/add/"), {"name": "Brand new"}
        )

        assert triggers(response)["showToast"]["tone"] == "success"
        assert crm["contact"].tags.filter(name="Brand new").exists()

    @pytest.mark.parametrize("role", ("agent", "viewer"))
    def test_deleting_a_contact_needs_manage_crm(self, tenancy, client_for, crm, role):
        response = client_for(tenancy.user_for(role)).post(url(tenancy, f"contacts/{crm['contact'].pk}/delete/"))

        assert response.status_code == 403
        crm["contact"].refresh_from_db()
        assert crm["contact"].status == ContactStatus.ACTIVE

    @pytest.mark.parametrize("role", MANAGERS)
    def test_editor_and_above_may_delete(self, tenancy, client_for, crm, role):
        response = client_for(tenancy.user_for(role)).post(url(tenancy, f"contacts/{crm['contact'].pk}/delete/"))

        assert response.status_code == 204
        crm["contact"].refresh_from_db()
        assert crm["contact"].status == ContactStatus.DELETED

    @pytest.mark.parametrize("role", ("agent", "viewer"))
    def test_exporting_needs_manage_crm(self, tenancy, client_for, role):
        """Reading a page of contacts and walking away with every contact's PII
        in one file are not the same act. Viewer is read-only, not read-and-take."""
        response = client_for(tenancy.user_for(role)).get(url(tenancy, "contacts/export/"))

        assert response.status_code == 403

    @pytest.mark.parametrize("role", ("agent", "viewer"))
    def test_the_import_wizard_needs_manage_crm(self, tenancy, client_for, role):
        response = client_for(tenancy.user_for(role)).get(url(tenancy, "contacts/import/"))

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestListSearchAndSort:
    def test_search_matches_name_email_and_phone(self, tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Grace", email="grace@navy.test")
        services.create_contact(tenancy.workspace, first_name="Alan", phone="+445550101")
        client = client_for(tenancy.owner)

        for term, present, absent in [
            ("grace", "Grace", "Alan"),
            ("navy.test", "Grace", "Alan"),
            ("5550101", "Alan", "Grace"),
        ]:
            body = client.get(url(tenancy, f"contacts/rows/?q={term}")).content.decode()
            assert present in body, term
            assert absent not in body, term

    def test_an_unknown_sort_key_falls_back_instead_of_reaching_order_by(self, tenancy, client_for):
        """``sort`` is a key into a frozen dict, never an ``order_by`` argument.
        A hostile value therefore cannot name a column, let alone a related one."""
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/?sort=workspace__organization__name"))

        assert response.status_code == 200

    def test_the_rows_endpoint_pushes_the_page_url_not_its_own(self, tenancy, client_for):
        """Otherwise htmx bookmarks the partial and the reader gets a bare table."""
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/?q=ada"))

        pushed = response.headers["HX-Push-Url"]
        assert pushed.startswith(url(tenancy, "contacts/"))
        assert "rows" not in pushed
        assert "q=ada" in pushed

    def test_sorting_by_name_orders_by_name(self, tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Zoe")
        services.create_contact(tenancy.workspace, first_name="Alan")

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/?sort=name")).content.decode()

        assert body.index("Alan") < body.index("Zoe")


@pytest.mark.django_db
class TestListFiltering:
    def _tagged(self, tenancy, tag, name):
        contact = services.create_contact(tenancy.workspace, first_name=name)
        services.add_tag(contact, tag)
        return contact

    def test_a_filter_document_narrows_the_list_through_the_condition_engine(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        self._tagged(tenancy, tag, "Insider")
        services.create_contact(tenancy.workspace, first_name="Outsider")
        document = json.dumps({"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]})

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"filter": document}).content.decode()

        assert "Insider" in body
        assert "Outsider" not in body

    def test_another_tenants_tag_id_inside_a_filter_matches_nothing_and_does_not_500(
        self, tenancy, other_tenancy, client_for
    ):
        """The sweep in tests/idor.py walks URL kwargs; this id rides in a JSON
        document in the query string. Resolution is scoped, so the id is simply
        not found — "unknown", not "forbidden" (SECURITY-BASELINE §1)."""
        theirs, _ = services.get_or_create_tag(other_tenancy.workspace, "Theirs")
        services.create_contact(tenancy.workspace, first_name="Mine")
        document = json.dumps({"match": "all", "rules": [{"source": "tag", "key": str(theirs.pk), "op": "has"}]})

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"filter": document})

        assert response.status_code == 200
        body = response.content.decode()
        assert "Mine" not in body
        assert "no such tag" in body

    def test_a_malformed_filter_fails_closed_rather_than_showing_everyone(self, tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Everyone")

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/?filter=not-json")).content.decode()

        assert "Everyone" not in body
        assert "could not be applied" in body

    def test_an_oversized_filter_is_refused_before_it_is_parsed(self, tenancy, client_for):
        from apps.contacts.conditions import MAX_FILTER_BYTES

        response = client_for(tenancy.owner).get(
            url(tenancy, "contacts/rows/"), {"filter": "x" * (MAX_FILTER_BYTES + 1)}
        )

        assert response.status_code == 200
        assert "too large" in response.content.decode()


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSegments:
    """The issue's headline acceptance criterion: save a filter, reload it, and
    the rules must be identical."""

    DOCUMENT = {
        "match": "any",
        "rules": [
            {"source": "system_field", "key": "email", "op": "contains", "value": "example.test"},
            {
                "source": "system_field",
                "key": "created_at",
                "op": "after",
                "value": {"relative": {"unit": "days", "offset": -7}},
            },
        ],
    }

    def test_a_saved_filter_round_trips_byte_for_byte(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        response = client.post(
            url(tenancy, "contacts/segments/create/"),
            {"name": "Recent example.test", "filter": json.dumps(self.DOCUMENT)},
        )
        assert triggers(response)["showToast"]["tone"] == "success"

        segment = Segment.objects.for_workspace(tenancy.workspace).get(name="Recent example.test")
        assert segment.filter_json == self.DOCUMENT

    def test_reloading_the_segment_hands_the_builder_the_same_document(self, tenancy, client_for):
        """Not just the row: the payload the filter builder hydrates from. A view
        that re-serialised on the way out would pass the row check and still show
        the operator different rules than they saved."""
        segment = services.create_segment(tenancy.workspace, name="Round trip", filter_json=self.DOCUMENT)

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/?segment={segment.pk}"))

        assert response.context["filter_config"]["document"] == self.DOCUMENT
        assert response.context["filter_config"]["segmentId"] == str(segment.pk)

    def test_the_builder_payload_comes_from_the_condition_engines_own_tables(self, tenancy, client_for):
        """No second operator table. If CONDITION_SCHEMA gains an operator, this
        page offers it with no edit — which is what the assertion pins."""
        from apps.contacts.conditions import CONDITION_SCHEMA, SOURCE_NAMES

        config = client_for(tenancy.owner).get(url(tenancy, "contacts/")).context["filter_config"]

        assert config["vocabulary"] is CONDITION_SCHEMA["x-brightbean"]
        assert [source["name"] for source in config["sources"]] == list(SOURCE_NAMES)

    def test_an_unimplemented_source_is_offered_but_marked(self, tenancy, client_for):
        """`sequence` validates and can be saved but cannot be evaluated until
        issue #22. The builder greys it out and names the owner rather than
        hiding it, so the vocabulary the schema describes stays visible."""
        config = client_for(tenancy.owner).get(url(tenancy, "contacts/")).context["filter_config"]
        sequence = next(source for source in config["sources"] if source["name"] == "sequence")

        assert sequence["evaluable"] is False
        assert "#22" in sequence["owner"]

    def test_saving_a_filter_that_will_not_validate_is_refused(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            url(tenancy, "contacts/segments/create/"),
            {
                "name": "Bad",
                "filter": json.dumps({"match": "all", "rules": [{"source": "nope", "key": "x", "op": "is"}]}),
            },
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        assert not Segment.objects.for_workspace(tenancy.workspace).exists()

    def test_another_tenants_segment_cannot_be_renamed_or_deleted(self, tenancy, other_tenancy, client_for):
        theirs = services.create_segment(
            other_tenancy.workspace, name="Theirs", filter_json={"match": "all", "rules": []}
        )
        client = client_for(tenancy.owner)

        assert client.post(url(tenancy, f"contacts/segments/{theirs.pk}/save/"), {"name": "Mine"}).status_code == 404
        assert client.post(url(tenancy, f"contacts/segments/{theirs.pk}/delete/")).status_code == 404
        theirs.refresh_from_db()
        assert theirs.name == "Theirs"

    @pytest.mark.parametrize("role", ("agent", "viewer"))
    def test_saving_a_segment_needs_manage_crm(self, tenancy, client_for, role):
        response = client_for(tenancy.user_for(role)).post(
            url(tenancy, "contacts/segments/create/"), {"name": "Nope", "filter": json.dumps(self.DOCUMENT)}
        )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestBulkActions:
    def test_tagging_a_selection_goes_through_the_service_so_events_fire(self, tenancy, client_for):
        """Row by row rather than a bulk insert: contact.tag_added is what issue
        #22's rule triggers and #25's webhooks subscribe to, and a link row
        inserted behind the services layer is a change nothing else learns about."""
        from apps.contacts.events import EVENT_CATALOG, EVENT_CONTACT_TAG_ADDED

        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        one = services.create_contact(tenancy.workspace, first_name="One")
        two = services.create_contact(tenancy.workspace, first_name="Two")
        seen: list = []
        EVENT_CATALOG[EVENT_CONTACT_TAG_ADDED].connect(lambda **kw: seen.append(kw["contact_id"]), weak=False)

        response = client_for(tenancy.owner).post(
            url(tenancy, "contacts/bulk/tag/"),
            {"tag_id": str(tag.pk), "mode": "add", "ids": [str(one.pk), str(two.pk)]},
        )

        assert response.status_code == 204
        assert set(seen) == {one.pk, two.pk}

    def test_ids_belonging_to_another_workspace_are_simply_absent(self, tenancy, other_tenancy, client_for):
        """The sweep cannot see these — they arrive in the POST body. Scoping
        makes a foreign id a miss rather than a refusal."""
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        theirs = services.create_contact(other_tenancy.workspace, first_name="Theirs")

        response = client_for(tenancy.owner).post(
            url(tenancy, "contacts/bulk/tag/"), {"tag_id": str(tag.pk), "mode": "add", "ids": [str(theirs.pk)]}
        )

        assert response.status_code == 204
        assert triggers(response)["showToast"]["title"] == "Nothing selected"
        assert theirs.tags.count() == 0

    def test_a_malformed_id_in_the_selection_does_not_500(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        contact = services.create_contact(tenancy.workspace, first_name="Real")

        response = client_for(tenancy.owner).post(
            url(tenancy, "contacts/bulk/tag/"),
            {"tag_id": str(tag.pk), "mode": "add", "ids": ["not-a-uuid", str(contact.pk)]},
        )

        assert response.status_code == 204
        assert contact.tags.count() == 1

    def test_the_selection_is_capped(self, tenancy, client_for):
        """A hand-made POST cannot ask one web request to touch the whole
        workspace; targeting a segment is a broadcast's shape (issue #23)."""
        from apps.contacts.views import MAX_BULK_IDS

        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        contacts = [services.create_contact(tenancy.workspace, first_name=f"C{i}") for i in range(3)]
        ids = ["00000000-0000-0000-0000-000000000000"] * MAX_BULK_IDS + [str(c.pk) for c in contacts]

        client_for(tenancy.owner).post(
            url(tenancy, "contacts/bulk/tag/"), {"tag_id": str(tag.pk), "mode": "add", "ids": ids}
        )

        assert all(contact.tags.count() == 0 for contact in contacts)

    def test_bulk_delete_stops_automation_first(self, tenancy, client_for, monkeypatch):
        """A tombstone with a live execution keeps sending to somebody every
        surface has stopped showing."""
        from apps.contacts import views

        stopped: list = []
        monkeypatch.setattr(views.activity, "stop_automation", lambda contact: stopped.append(contact.pk) or 0)
        contact = services.create_contact(tenancy.workspace, first_name="Doomed")

        client_for(tenancy.owner).post(url(tenancy, "contacts/bulk/delete/"), {"ids": [str(contact.pk)]})

        assert stopped == [contact.pk]
        contact.refresh_from_db()
        assert contact.status == ContactStatus.DELETED

    def test_the_sequence_endpoint_is_a_polite_no_op_until_l6a(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(url(tenancy, "contacts/bulk/sequence/"), {"ids": []})

        assert response.status_code == 204
        assert "#22" in triggers(response)["showToast"]["body"]


# ---------------------------------------------------------------------------
# The detail page
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestContactDetail:
    def test_a_soft_deleted_contact_is_a_404_not_a_read_only_page(self, tenancy, client_for, crm):
        """Otherwise a tombstone stays editable, and tagging one puts somebody
        the operator removed back into a segment."""
        services.delete_contact(crm["contact"])

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))

        assert response.status_code == 404

    def test_only_the_submitted_field_is_written(self, tenancy, client_for, crm):
        """Each field posts on its own, so the other five must not arrive empty
        and clear themselves."""
        client_for(tenancy.owner).post(url(tenancy, f"contacts/{crm['contact'].pk}/edit/"), {"phone": "+445550111"})

        crm["contact"].refresh_from_db()
        assert crm["contact"].phone == "+445550111"
        assert crm["contact"].first_name == "Ada"
        assert crm["contact"].email == "ada@x.test"

    def test_a_field_outside_the_allowlist_is_refused_rather_than_ignored(self, tenancy, client_for, crm):
        """`status` and `last_interaction_at` are columns on this model that a
        mass assignment would otherwise reach."""
        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/edit/"), {"status": ContactStatus.DELETED}
        )

        assert triggers(response)["showToast"]["title"] == "Nothing to save"
        crm["contact"].refresh_from_db()
        assert crm["contact"].status == ContactStatus.ACTIVE

    def test_emptying_a_custom_field_removes_the_row_rather_than_storing_blank(self, tenancy, client_for, crm):
        """clear_field_value deletes the row, which is what keeps the check
        constraint's "exactly one column populated" true and what the condition
        engine's no_value operator means."""
        from apps.contacts.models import CustomFieldValue

        services.set_field_value(crm["contact"], crm["field"], "Pro")
        client = client_for(tenancy.owner)

        client.post(url(tenancy, f"contacts/{crm['contact'].pk}/fields/{crm['field'].pk}/"), {"value": ""})

        assert not CustomFieldValue.objects.for_workspace(tenancy.workspace).filter(contact=crm["contact"]).exists()

    def test_a_wrongly_typed_custom_field_value_is_a_toast_not_a_500(self, tenancy, client_for, crm):
        number = services.create_custom_field(tenancy.workspace, name="Seats", field_type=CustomFieldType.NUMBER)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/fields/{number.pk}/"), {"value": "lots"}
        )

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"

    def test_a_boolean_field_stores_false_rather_than_reading_absence_as_clear(self, tenancy, client_for, crm):
        flag = services.create_custom_field(tenancy.workspace, name="Trial", field_type=CustomFieldType.BOOLEAN)

        client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/fields/{flag.pk}/"), {"value": "false"}
        )

        assert services.field_values_for(crm["contact"])[flag.pk] is False

    def test_a_custom_field_from_another_workspace_is_a_404(self, tenancy, other_tenancy, client_for, crm):
        theirs = services.create_custom_field(other_tenancy.workspace, name="Theirs", field_type=CustomFieldType.TEXT)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/{crm['contact'].pk}/fields/{theirs.pk}/"), {"value": "x"}
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestDeletingAContact:
    def test_it_soft_deletes_and_stops_automation(self, tenancy, client_for, crm, monkeypatch):
        from apps.contacts import views

        stopped: list = []
        monkeypatch.setattr(views.activity, "stop_automation", lambda contact: stopped.append(contact.pk) or 0)

        client_for(tenancy.owner).post(url(tenancy, f"contacts/{crm['contact'].pk}/delete/"))

        crm["contact"].refresh_from_db()
        assert crm["contact"].status == ContactStatus.DELETED
        assert stopped == [crm["contact"].pk]

    def test_a_deleted_contact_leaves_every_read_surface(self, tenancy, client_for, crm):
        client = client_for(tenancy.owner)
        client.post(url(tenancy, f"contacts/{crm['contact'].pk}/delete/"))

        assert "Ada" not in client.get(url(tenancy, "contacts/")).content.decode()

    def test_deleting_is_idempotent(self, tenancy, client_for, crm):
        services.delete_contact(crm["contact"])

        response = client_for(tenancy.owner).post(url(tenancy, f"contacts/{crm['contact'].pk}/delete/"))

        assert response.status_code == 404


@pytest.mark.django_db
class TestTagMerge:
    def test_links_move_and_the_source_goes(self, tenancy, client_for):
        keep, _ = services.get_or_create_tag(tenancy.workspace, "Priority")
        drop, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        both = services.create_contact(tenancy.workspace, first_name="Both")
        only_drop = services.create_contact(tenancy.workspace, first_name="Only")
        services.add_tag(both, keep)
        services.add_tag(both, drop)
        services.add_tag(only_drop, drop)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"settings/tags/{drop.pk}/merge/"), {"target_id": str(keep.pk)}
        )

        assert response.status_code == 204
        assert not Tag.objects.for_workspace(tenancy.workspace).filter(pk=drop.pk).exists()
        assert set(both.tags.values_list("name", flat=True)) == {"Priority"}
        assert set(only_drop.tags.values_list("name", flat=True)) == {"Priority"}

    def test_merging_a_tag_into_itself_is_refused(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"settings/tags/{tag.pk}/merge/"), {"target_id": str(tag.pk)}
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        assert Tag.objects.for_workspace(tenancy.workspace).filter(pk=tag.pk).exists()

    def test_merging_into_another_workspaces_tag_is_a_404(self, tenancy, other_tenancy, client_for):
        mine, _ = services.get_or_create_tag(tenancy.workspace, "Mine")
        theirs, _ = services.get_or_create_tag(other_tenancy.workspace, "Theirs")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"settings/tags/{mine.pk}/merge/"), {"target_id": str(theirs.pk)}
        )

        assert response.status_code == 404
        assert Tag.objects.for_workspace(tenancy.workspace).filter(pk=mine.pk).exists()


@pytest.mark.django_db
class TestSegmentUpdate:
    def test_a_segment_can_be_renamed_without_touching_its_rules(self, tenancy, client_for):
        segment = services.create_segment(tenancy.workspace, name="Old name", filter_json={"match": "all", "rules": []})

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/segments/{segment.pk}/save/"), {"name": "New name"}
        )

        assert response.status_code == 204
        segment.refresh_from_db()
        assert segment.name == "New name"
        assert segment.filter_json == {"match": "all", "rules": []}

    def test_its_rules_can_be_replaced(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        segment = services.create_segment(tenancy.workspace, name="Everyone", filter_json={"match": "all", "rules": []})
        document = {"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]}

        client_for(tenancy.owner).post(
            url(tenancy, f"contacts/segments/{segment.pk}/save/"),
            {"name": segment.name, "filter": json.dumps(document)},
        )

        segment.refresh_from_db()
        assert segment.filter_json == document

    def test_a_segment_cannot_be_made_to_reference_itself(self, tenancy, client_for):
        """``update_segment`` seeds the cycle detector with the row being saved,
        so a self-reference dies before it can be stored and re-read forever."""
        segment = services.create_segment(tenancy.workspace, name="Self", filter_json={"match": "all", "rules": []})
        document = {"match": "all", "rules": [{"source": "segment", "key": str(segment.pk), "op": "in"}]}

        response = client_for(tenancy.owner).post(
            url(tenancy, f"contacts/segments/{segment.pk}/save/"),
            {"name": "Self", "filter": json.dumps(document)},
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        segment.refresh_from_db()
        assert segment.filter_json == {"match": "all", "rules": []}

    def test_deleting_a_segment_leaves_its_contacts_alone(self, tenancy, client_for):
        """A segment is a saved question, not a container."""
        services.create_contact(tenancy.workspace, first_name="Ada")
        segment = services.create_segment(tenancy.workspace, name="Doomed", filter_json={"match": "all", "rules": []})

        client_for(tenancy.owner).post(url(tenancy, f"contacts/segments/{segment.pk}/delete/"))

        assert not Segment.objects.for_workspace(tenancy.workspace).exists()
        assert Contact.objects.for_workspace(tenancy.workspace).count() == 1


@pytest.mark.django_db
class TestListShape:
    def test_the_default_ordering_puts_never_seen_contacts_last(self, tenancy, client_for):
        """Postgres sorts NULL above every value under DESC, so a plain
        `-last_interaction_at` puts everyone who has never interacted at the top
        of a list whose whole point is recency."""
        now = timezone.now()
        services.create_contact(tenancy.workspace, first_name="Never")
        services.create_contact(tenancy.workspace, first_name="Recent", last_interaction_at=now)
        services.create_contact(tenancy.workspace, first_name="Older", last_interaction_at=now - timedelta(days=5))

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/")).content.decode()

        assert body.index("Recent") < body.index("Older") < body.index("Never")

    def test_the_export_link_carries_the_current_view(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        document = json.dumps({"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]})

        body = (
            client_for(tenancy.owner).get(url(tenancy, "contacts/"), {"filter": document, "q": "ada"}).content.decode()
        )

        assert "contacts/export/?filter=" in body
        assert "q=ada" in body

    def test_a_viewer_sees_no_write_controls(self, tenancy, client_for, crm):
        """The template branches on the same flags the decorators enforce, so a
        button can never render for somebody who gets a 403 when they press it."""
        body = (
            client_for(tenancy.user_for("viewer")).get(url(tenancy, f"contacts/{crm['contact'].pk}/")).content.decode()
        )

        assert "Delete contact" not in body
        assert "Find or add a tag" not in body
        assert "Stop automation" not in body

    def test_an_agent_sees_the_field_editor_but_not_the_delete_button(self, tenancy, client_for, crm):
        body = (
            client_for(tenancy.user_for("agent")).get(url(tenancy, f"contacts/{crm['contact'].pk}/")).content.decode()
        )

        assert "Find or add a tag" in body
        assert "Delete contact" not in body


@pytest.mark.django_db
class TestContextKeys:
    def test_the_activity_pane_does_not_shadow_djangos_messages(self, tenancy, client_for, crm):
        """`messages` is the messages framework's name, supplied by a context
        processor and rendered by base.html as flash alerts. Binding the contact's
        recent messages to it put the repr of every MessagePreview in a banner
        across the top of the page — visible immediately, and invisible to a test
        that only asserts a 200."""
        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))

        assert "recent_messages" in response.context
        assert "MessagePreview(" not in response.content.decode()


@pytest.mark.django_db
class TestHostileQueryParameters:
    """The query string is the part ``tests/idor.py`` does not walk, and the part
    a crafted link reaches without a form."""

    def test_a_json_depth_bomb_in_the_filter_is_refused_not_a_500(self, tenancy, client_for):
        """16 KiB of `[` fits inside MAX_FILTER_BYTES, so only a depth check
        catches it — and the RecursionError it otherwise raises is not a
        ValueError, so it escaped as a 500 rather than a refusal."""
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"filter": "[" * 16000})

        assert response.status_code == 200
        assert "could not be applied" in response.content.decode()

    def test_the_depth_bomb_is_refused_on_the_export_too(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/export/"), {"filter": "[" * 16000})

        assert response.status_code == 404  # fails closed rather than exporting everyone

    def test_saving_a_segment_from_a_depth_bomb_is_refused(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            url(tenancy, "contacts/segments/create/"), {"name": "Bomb", "filter": "[" * 16000}
        )

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"
        assert not Segment.objects.for_workspace(tenancy.workspace).exists()

    def test_a_newline_in_the_page_parameter_does_not_500(self, tenancy, client_for):
        """It reached HX-Push-Url verbatim, and Django refuses a header value
        holding a newline — so a crafted link answered 500."""
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"page": "1\r\nX-Injected: yes"})

        assert response.status_code == 200
        assert "X-Injected" not in response.headers["HX-Push-Url"]

    def test_the_pushed_url_cannot_carry_smuggled_parameters(self, tenancy, client_for):
        """The page is taken as an int off the Paginator, so nothing the caller
        typed is interpolated into the URL htmx writes to the address bar."""
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"page": "9 &evil=1"})

        assert "evil" not in response.headers["HX-Push-Url"]

    def test_the_pushed_url_keeps_a_real_page(self, tenancy, client_for):
        for index in range(60):
            services.create_contact(tenancy.workspace, first_name=f"C{index:02d}")

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"page": "2"})

        assert "page=2" in response.headers["HX-Push-Url"]

    def test_page_one_is_not_spelled_in_the_url(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"))

        assert "page=" not in response.headers["HX-Push-Url"]


@pytest.mark.django_db
class TestTheRefreshKeepsTheView:
    def test_the_container_refetches_its_own_page_not_page_one(self, tenancy, client_for):
        """A bulk action fires contactsChanged and the container re-fetches from
        its own hx-get. Without the page in it, an operator acting on page 3
        watched the table jump back to page 1 underneath them."""
        for index in range(120):
            services.create_contact(tenancy.workspace, first_name=f"C{index:03d}")

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/rows/"), {"page": "2"}).content.decode()

        assert 'hx-trigger="contactsChanged from:body"' in body
        assert "page=2" in body

    def test_the_export_link_never_carries_a_page(self, tenancy, client_for):
        """ "Export the current filter" is about the set, not the screenful."""
        for index in range(60):
            services.create_contact(tenancy.workspace, first_name=f"C{index:02d}")

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/"), {"page": "2"}).content.decode()

        assert "contacts/export/" in body
        export_line = next(line for line in body.splitlines() if "contacts/export/" in line)
        assert "page=" not in export_line


@pytest.mark.django_db
class TestNumberFieldRendering:
    def test_a_whole_number_is_not_shown_with_six_decimal_places(self, tenancy, client_for, crm):
        """value_number is a DecimalField(decimal_places=6), so str() on a stored
        5 gives "5.000000" — a number the operator did not type, in the box they
        are about to edit."""
        field = services.create_custom_field(tenancy.workspace, name="Seats", field_type=CustomFieldType.NUMBER)
        services.set_field_value(crm["contact"], field, 5)

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))
        row = next(r for r in response.context["field_values"] if r["field"].pk == field.pk)

        assert row["form_value"] == "5"

    def test_a_round_hundred_does_not_become_scientific_notation(self, tenancy, client_for, crm):
        """Decimal.normalize() turns 100 into 1E+2, which an input[type=number]
        shows verbatim."""
        field = services.create_custom_field(tenancy.workspace, name="Seats", field_type=CustomFieldType.NUMBER)
        services.set_field_value(crm["contact"], field, 100)

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))
        row = next(r for r in response.context["field_values"] if r["field"].pk == field.pk)

        assert row["form_value"] == "100"

    def test_a_real_fraction_keeps_its_digits(self, tenancy, client_for, crm):
        field = services.create_custom_field(tenancy.workspace, name="Rate", field_type=CustomFieldType.NUMBER)
        services.set_field_value(crm["contact"], field, "1.25")

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/{crm['contact'].pk}/"))
        row = next(r for r in response.context["field_values"] if r["field"].pk == field.pk)

        assert row["form_value"] == "1.25"


@pytest.mark.django_db
class TestMergeCounts:
    def test_the_count_is_live_contacts_carrying_the_source(self, tenancy, client_for):
        """Not the write's row count. A contact who already carries the target
        has their source link deleted rather than moved, so the row count
        under-reports the people the merge affected."""
        keep, _ = services.get_or_create_tag(tenancy.workspace, "Priority")
        drop, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        for name in ("Both", "Also both"):
            contact = services.create_contact(tenancy.workspace, first_name=name)
            services.add_tag(contact, keep)
            services.add_tag(contact, drop)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"settings/tags/{drop.pk}/merge/"), {"target_id": str(keep.pk)}
        )

        assert "2 contacts moved" in triggers(response)["showToast"]["body"]

    def test_the_count_excludes_soft_deleted_contacts(self, tenancy, client_for):
        """The trap delete_tag documents: a number about people must not include
        people the rest of the app has stopped showing."""
        keep, _ = services.get_or_create_tag(tenancy.workspace, "Priority")
        drop, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        live = services.create_contact(tenancy.workspace, first_name="Live")
        gone = services.create_contact(tenancy.workspace, first_name="Gone")
        services.add_tag(live, drop)
        services.add_tag(gone, drop)
        services.delete_contact(gone)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"settings/tags/{drop.pk}/merge/"), {"target_id": str(keep.pk)}
        )

        assert "1 contact moved" in triggers(response)["showToast"]["body"]
        # The tombstone's link still moves — it belongs to that contact, and
        # issue #29's export has to be able to read it.
        assert set(gone.tags.values_list("name", flat=True)) == {"Priority"}


@pytest.mark.django_db
class TestSegmentPickerReusesOneQuery:
    def test_the_picker_and_the_builder_read_the_same_list(self, tenancy, client_for):
        services.create_segment(tenancy.workspace, name="VIPs", filter_json={"match": "all", "rules": []})

        response = client_for(tenancy.owner).get(url(tenancy, "contacts/"))

        assert "segment_rows" not in response.context
        assert [row["label"] for row in response.context["filter_config"]["segments"]] == ["VIPs"]
        assert "VIPs" in response.content.decode()
