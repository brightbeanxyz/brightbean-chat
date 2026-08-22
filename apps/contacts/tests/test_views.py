"""The two **settings** pages: role gating, HTMX responses, tenancy.

L2-A shipped this file covering three pages. The contact list moved out with
issue #13 — it is no longer gated the same way, and its own suite is
``test_crm.py`` — so what is left here is tags and custom fields, which are
still ``manage_crm`` throughout.

The cross-tenant sweep in ``tests/idor.py`` covers every URL kwarg
automatically. What it cannot see is a tenant id in a query string or a POST
body; those gaps are tested directly, here and in ``test_crm.py``.
"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from apps.contacts import services
from apps.contacts.models import ContactStatus, CustomField, CustomFieldType, Segment, Tag

ALLOWED_ROLES = ("admin", "editor")
REFUSED_ROLES = ("agent", "viewer")


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/{suffix}"


#: The pages that are still ``manage_crm`` end to end. ``contacts/`` is
#: deliberately absent: issue #13 moved the CRM's *read* gate down to "is a
#: member of this workspace" so an Agent can edit a contact's tags and fields and
#: a Viewer can see the list read-only, which SPEC §4's table requires and none
#: of its nine permission keys expresses. ``test_crm.py`` owns that matrix.
PAGES = ("settings/tags/", "settings/fields/")


def triggers(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])


@pytest.mark.django_db
class TestAccessControl:
    @pytest.mark.parametrize("page", PAGES)
    @pytest.mark.parametrize("role", ALLOWED_ROLES)
    def test_manage_crm_holders_can_open_every_page(self, tenancy, client_for, page, role):
        response = client_for(tenancy.user_for(role)).get(url(tenancy, page))

        assert response.status_code == 200

    @pytest.mark.parametrize("page", PAGES)
    @pytest.mark.parametrize("role", REFUSED_ROLES)
    def test_everyone_else_is_refused(self, tenancy, client_for, page, role):
        response = client_for(tenancy.user_for(role)).get(url(tenancy, page))

        assert response.status_code == 403

    @pytest.mark.parametrize("page", PAGES)
    def test_an_anonymous_visitor_is_sent_to_login(self, tenancy, client, page):
        response = client.get(url(tenancy, page))

        assert response.status_code == 302
        assert "/accounts/login/" in response.headers["Location"]

    def test_a_get_on_a_post_only_route_is_a_405(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(url(tenancy, "settings/tags/create/"))

        assert response.status_code == 405


@pytest.mark.django_db
class TestTagSettings:
    def test_creating_a_tag_toasts_and_asks_the_list_to_refresh(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(url(tenancy, "settings/tags/create/"), {"name": "VIP"})

        assert response.status_code == 204
        assert triggers(response)["tagsChanged"] is True
        assert triggers(response)["showToast"]["tone"] == "success"
        assert Tag.objects.for_workspace(tenancy.workspace).count() == 1

    def test_creating_a_tag_that_already_exists_says_so_without_duplicating(self, tenancy, client_for):
        services.get_or_create_tag(tenancy.workspace, "VIP")

        response = client_for(tenancy.owner).post(url(tenancy, "settings/tags/create/"), {"name": "vip"})

        assert triggers(response)["showToast"]["tone"] == "info"
        assert Tag.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_blank_name_is_reported_as_an_error_toast(self, tenancy, client_for):
        """Still a 204: htmx does not process HX-Trigger on a non-2xx, so a 400
        would swallow the message the user needs to read."""
        response = client_for(tenancy.owner).post(url(tenancy, "settings/tags/create/"), {"name": "  "})

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"

    def test_renaming_and_deleting(self, tenancy, client_for, contact):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        services.add_tag(contact, tag)
        client = client_for(tenancy.owner)

        client.post(url(tenancy, f"settings/tags/{tag.pk}/rename/"), {"name": "Gold"})
        tag.refresh_from_db()
        assert tag.name == "Gold"

        response = client.post(url(tenancy, f"settings/tags/{tag.pk}/delete/"))
        assert "1 contact" in triggers(response)["showToast"]["body"]
        assert Tag.objects.for_workspace(tenancy.workspace).count() == 0

    def test_the_list_shows_how_many_contacts_carry_each_tag(self, tenancy, client_for, contact):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        services.add_tag(contact, tag)

        body = client_for(tenancy.owner).get(url(tenancy, "settings/tags/")).content.decode()

        assert "VIP" in body

    @pytest.mark.parametrize("action", ["rename/", "delete/"])
    def test_another_workspaces_tag_is_a_404(self, tenancy, other_tenancy, client_for, action):
        theirs, _ = services.get_or_create_tag(other_tenancy.workspace, "theirs")

        response = client_for(tenancy.owner).post(url(tenancy, f"settings/tags/{theirs.pk}/{action}"), {"name": "x"})

        assert response.status_code == 404


@pytest.mark.django_db
class TestFieldSettings:
    def test_creating_a_field_of_each_type(self, tenancy, client_for):
        client = client_for(tenancy.owner)
        for index, (value, _label) in enumerate(CustomFieldType.choices):
            response = client.post(
                url(tenancy, "settings/fields/create/"), {"name": f"Field {index}", "field_type": value}
            )
            assert triggers(response)["fieldsChanged"] is True

        assert CustomField.objects.for_workspace(tenancy.workspace).count() == len(CustomFieldType.choices)

    def test_an_unknown_type_is_refused(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(
            url(tenancy, "settings/fields/create/"), {"name": "Plan", "field_type": "telepathy"}
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        assert CustomField.objects.for_workspace(tenancy.workspace).count() == 0

    def test_a_duplicate_name_is_refused(self, tenancy, client_for, custom_field):
        response = client_for(tenancy.owner).post(
            url(tenancy, "settings/fields/create/"), {"name": "plan", "field_type": "text"}
        )

        assert triggers(response)["showToast"]["tone"] == "error"

    def test_renaming_and_deleting(self, tenancy, client_for, contact, custom_field):
        services.set_field_value(contact, custom_field, "gold")
        client = client_for(tenancy.owner)

        client.post(url(tenancy, f"settings/fields/{custom_field.pk}/rename/"), {"name": "Tier"})
        custom_field.refresh_from_db()
        assert custom_field.name == "Tier"

        response = client.post(url(tenancy, f"settings/fields/{custom_field.pk}/delete/"))
        assert "1 stored value" in triggers(response)["showToast"]["body"]

    def test_another_workspaces_field_is_a_404(self, tenancy, other_tenancy, client_for):
        theirs = CustomField.objects.create(workspace=other_tenancy.workspace, name="Plan", type=CustomFieldType.TEXT)

        response = client_for(tenancy.owner).post(url(tenancy, f"settings/fields/{theirs.pk}/rename/"), {"name": "x"})

        assert response.status_code == 404


@pytest.mark.django_db
class TestTheContactList:
    def test_it_shows_only_this_workspaces_contacts(self, tenancy, other_tenancy, client_for):
        services.create_contact(tenancy.workspace, first_name="Mine")
        services.create_contact(other_tenancy.workspace, first_name="Theirs")

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/")).content.decode()

        assert "Mine" in body
        assert "Theirs" not in body

    def test_it_hides_soft_deleted_contacts(self, tenancy, client_for):
        gone = services.create_contact(tenancy.workspace, first_name="Tombstone")
        gone.status = ContactStatus.DELETED
        gone.save(update_fields=["status"])

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/")).content.decode()

        assert "Tombstone" not in body

    def test_a_segment_narrows_the_list(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        inside = services.create_contact(tenancy.workspace, first_name="Insider")
        services.create_contact(tenancy.workspace, first_name="Outsider")
        services.add_tag(inside, tag)
        segment = services.create_segment(
            tenancy.workspace,
            name="VIPs",
            filter_json={"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]},
        )

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/?segment={segment.pk}")).content.decode()

        assert "Insider" in body
        assert "Outsider" not in body

    def test_another_tenants_segment_id_is_a_404(self, tenancy, other_tenancy, client_for):
        """The gap tests/idor.py cannot see: it walks URL kwargs, and this is a
        query parameter."""
        theirs = services.create_segment(
            other_tenancy.workspace, name="Theirs", filter_json={"match": "all", "rules": []}
        )

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/?segment={theirs.pk}"))

        assert response.status_code == 404

    def test_a_malformed_segment_id_is_a_404(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(url(tenancy, "contacts/?segment=not-a-uuid"))

        assert response.status_code == 404

    def test_a_segment_the_engine_cannot_evaluate_degrades_instead_of_500ing(self, tenancy, client_for):
        """A saved filter can outlive what it references, or name a source this
        deployment has not implemented yet."""
        services.create_contact(tenancy.workspace, first_name="Someone")
        segment = Segment.objects.create(
            workspace=tenancy.workspace,
            name="Unimplemented source",
            # `sequence` is the remaining unimplemented slot (issue #22). This
            # used to be `window`, which issue #8 filled — the assertion is
            # about a source with no implementation, not about that source.
            filter_json={
                "match": "all",
                "rules": [{"source": "sequence", "key": str(uuid4()), "op": "subscribed"}],
            },
        )

        response = client_for(tenancy.owner).get(url(tenancy, f"contacts/?segment={segment.pk}"))

        assert response.status_code == 200
        assert "could not be applied" in response.content.decode()

    def test_the_list_does_not_run_a_query_per_row_for_tags(self, tenancy, client_for):
        """The tag column is prefetched, so the query count is flat in the row
        count. Asserting the *shape* rather than a magic number, which would
        otherwise turn red every time the shell adds a nav query."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        client = client_for(tenancy.owner)

        def render_with(count: int) -> int:
            for index in range(count):
                contact = services.create_contact(tenancy.workspace, first_name=f"C{index}-{count}")
                services.add_tag(contact, tag)
            client.get(url(tenancy, "contacts/"))  # warm session and nav queries
            with CaptureQueriesContext(connection) as captured:
                client.get(url(tenancy, "contacts/"))
            return len(captured.captured_queries)

        assert render_with(5) == render_with(25)


@pytest.mark.django_db
class TestTheContactListOrdering:
    def test_contacts_with_no_interaction_sort_last_not_first(self, tenancy, client_for):
        """Postgres sorts NULL above every value under DESC, so a plain
        `-last_interaction_at` puts everyone who has never interacted at the top
        of a list whose whole point is recency."""
        now = timezone.now()
        never = services.create_contact(tenancy.workspace, first_name="Never")
        recent = services.create_contact(tenancy.workspace, first_name="Recent", last_interaction_at=now)
        older = services.create_contact(
            tenancy.workspace, first_name="Older", last_interaction_at=now - timedelta(days=5)
        )

        body = client_for(tenancy.owner).get(url(tenancy, "contacts/")).content.decode()

        assert body.index("Recent") < body.index("Older") < body.index("Never")
        assert {recent.pk, older.pk, never.pk}  # all three rendered


@pytest.mark.django_db
class TestCountsExcludeTombstones:
    def _tombstone(self, contact):
        contact.status = ContactStatus.DELETED
        contact.save(update_fields=["status"])

    def test_a_tags_contact_count_ignores_soft_deleted_contacts(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        live = services.create_contact(tenancy.workspace, first_name="Live")
        gone = services.create_contact(tenancy.workspace, first_name="Gone")
        services.add_tag(live, tag)
        services.add_tag(gone, tag)
        self._tombstone(gone)

        body = client_for(tenancy.owner).get(url(tenancy, "settings/tags/")).content.decode()

        assert ">1<" in body.replace(" ", "").replace("\n", "")

    def test_a_fields_value_count_ignores_soft_deleted_contacts(self, tenancy, client_for, custom_field):
        live = services.create_contact(tenancy.workspace, first_name="Live")
        gone = services.create_contact(tenancy.workspace, first_name="Gone")
        services.set_field_value(live, custom_field, "a")
        services.set_field_value(gone, custom_field, "b")
        self._tombstone(gone)

        body = client_for(tenancy.owner).get(url(tenancy, "settings/fields/")).content.decode()

        assert ">1<" in body.replace(" ", "").replace("\n", "")

    def test_the_delete_toast_counts_live_contacts_only(self, tenancy, client_for):
        tag, _ = services.get_or_create_tag(tenancy.workspace, "VIP")
        live = services.create_contact(tenancy.workspace, first_name="Live")
        gone = services.create_contact(tenancy.workspace, first_name="Gone")
        services.add_tag(live, tag)
        services.add_tag(gone, tag)
        self._tombstone(gone)

        response = client_for(tenancy.owner).post(url(tenancy, f"settings/tags/{tag.pk}/delete/"))

        assert "1 contact." in triggers(response)["showToast"]["body"]


@pytest.mark.django_db
class TestASegmentThatCannotBeEvaluatedFailsClosed:
    def test_it_shows_no_contacts_rather_than_all_of_them(self, tenancy, client_for):
        """The operator asked for a subset; answering with the whole workspace
        is the least safe way to be wrong."""
        services.create_contact(tenancy.workspace, first_name="Zebediah")
        segment = Segment.objects.create(
            workspace=tenancy.workspace,
            name="Unimplemented source",
            # `sequence` is the remaining unimplemented slot (issue #22). This
            # used to be `window`, which issue #8 filled — the assertion is
            # about a source with no implementation, not about that source.
            filter_json={
                "match": "all",
                "rules": [{"source": "sequence", "key": str(uuid4()), "op": "subscribed"}],
            },
        )

        body = client_for(tenancy.owner).get(url(tenancy, f"contacts/?segment={segment.pk}")).content.decode()

        assert "could not be applied" in body
        # "Everyone" is the segment selector's null option, so assert on the
        # contact's own name rather than a word the chrome also uses.
        assert "Zebediah" not in body


@pytest.mark.django_db
class TestTheFragmentRoutes:
    @pytest.mark.parametrize("suffix", ["settings/tags/rows/", "settings/fields/rows/"])
    def test_a_fragment_renders_without_the_shell(self, tenancy, client_for, suffix):
        """The whole point: no sidebar, no workspace switcher, no shell queries."""
        body = client_for(tenancy.owner).get(url(tenancy, suffix)).content.decode()

        assert "sidebar-nav-item" not in body
        assert "<html" not in body

    @pytest.mark.parametrize("suffix", ["settings/tags/rows/", "settings/fields/rows/"])
    @pytest.mark.parametrize("role", REFUSED_ROLES)
    def test_a_fragment_is_gated_like_its_page(self, tenancy, client_for, suffix, role):
        assert client_for(tenancy.user_for(role)).get(url(tenancy, suffix)).status_code == 403

    def test_a_fragment_is_a_fraction_of_the_full_page(self, tenancy, client_for):
        """The saving is bytes and render work, not queries — sidebar_context is
        a context processor and runs for every render() whatever the template."""
        client = client_for(tenancy.owner)
        services.get_or_create_tag(tenancy.workspace, "VIP")

        page = client.get(url(tenancy, "settings/tags/")).content
        fragment = client.get(url(tenancy, "settings/tags/rows/")).content

        assert b"VIP" in fragment
        assert len(fragment) * 10 < len(page)
