"""The sequence pages: role gating, HTMX contracts, tenancy and hostile content.

The cross-tenant sweep in ``tests/idor.py`` covers every URL kwarg automatically
(``sequence_id``, ``step_id``, ``enrollment_id`` are registered there). What it
cannot see is a tenant id in a POST body, so the two endpoints that take one —
the step's ``flow_id`` and the subscriber panel's ``contact_id`` — are tested
directly here.
"""

import json

import pytest

from apps.campaigns import services
from apps.campaigns.models import EnrollmentStatus, Sequence, SequenceEnrollment, SequenceStatus, SequenceStep
from apps.campaigns.tests.support import contact_for, runnable_flow, sequence_with

#: ``edit_flows`` holders (SPEC §4). A sequence is a schedule over flows, so it
#: is gated by the same key the flow builder is.
ALLOWED_ROLES = ("admin", "editor")
READ_ONLY_ROLES = ("agent", "viewer")


def url(tenancy, suffix: str) -> str:
    return f"/w/{tenancy.workspace.id}/sequences/{suffix}"


def triggers(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])


@pytest.mark.django_db
class TestAccessControl:
    @pytest.mark.parametrize("role", (*ALLOWED_ROLES, *READ_ONLY_ROLES))
    def test_every_member_can_read_the_list(self, tenancy, client_for, role):
        assert client_for(tenancy.user_for(role)).get(url(tenancy, "")).status_code == 200

    @pytest.mark.parametrize("role", (*ALLOWED_ROLES, *READ_ONLY_ROLES))
    def test_every_member_can_read_the_editor(self, tenancy, client_for, role):
        sequence = sequence_with(tenancy.workspace, steps=1)

        assert client_for(tenancy.user_for(role)).get(url(tenancy, f"{sequence.pk}/")).status_code == 200

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_a_reader_may_not_create_one(self, tenancy, client_for, role):
        response = client_for(tenancy.user_for(role)).post(url(tenancy, "create/"), {"name": "Nope"})

        assert response.status_code == 403
        assert not Sequence.objects.for_workspace(tenancy.workspace).exists()

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_a_reader_may_not_subscribe_anyone(self, tenancy, client_for, role):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)

        response = client_for(tenancy.user_for(role)).post(
            url(tenancy, f"{sequence.pk}/subscribers/add/"), {"contact_id": str(contact.pk)}
        )

        assert response.status_code == 403

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_the_editor_hides_the_controls_a_reader_may_not_use(self, tenancy, client_for, role):
        sequence = sequence_with(tenancy.workspace, steps=1)

        body = client_for(tenancy.user_for(role)).get(url(tenancy, f"{sequence.pk}/")).content.decode()

        assert "Add a step" not in body


@pytest.mark.django_db
class TestTheList:
    def test_it_counts_steps_and_active_subscribers(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace, first_name="A"))
        gone = contact_for(tenancy.workspace, first_name="B")
        services.subscribe(sequence, gone)
        services.unsubscribe(sequence, gone)

        row = client_for(tenancy.owner).get(url(tenancy, "")).context["sequences"][0]

        assert row.step_count == 2
        assert row.subscriber_count == 1

    def test_htmx_gets_the_rows_partial_without_the_shell(self, tenancy, client_for):
        sequence_with(tenancy.workspace, steps=1)

        body = client_for(tenancy.owner).get(url(tenancy, ""), headers={"HX-Request": "true"}).content.decode()

        assert "<html" not in body
        assert "Onboarding" in body

    def test_the_search_box_filters(self, tenancy, client_for):
        sequence_with(tenancy.workspace, steps=1, name="Onboarding")
        sequence_with(tenancy.workspace, steps=1, name="Winback")

        names = [row.name for row in client_for(tenancy.owner).get(url(tenancy, "?q=wink")).context["sequences"]]

        assert names == []

    def test_a_hostile_name_is_escaped(self, tenancy, client_for):
        """Sequence names are user-authored text on the team-browser path
        (SECURITY-BASELINE §2)."""
        Sequence.objects.create(workspace=tenancy.workspace, name="<script>alert(1)</script>")

        body = client_for(tenancy.owner).get(url(tenancy, "")).content.decode()

        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


@pytest.mark.django_db
class TestMutations:
    def test_creating_one_answers_a_toast_and_a_refresh_event(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(url(tenancy, "create/"), {"name": "Onboarding"})

        assert triggers(response)["sequencesChanged"] is True
        assert Sequence.objects.for_workspace(tenancy.workspace).get().name == "Onboarding"

    def test_a_blank_name_is_refused_with_a_2xx_toast(self, tenancy, client_for):
        """htmx drops HX-Trigger on a non-2xx, so a 400 would show no toast."""
        response = client_for(tenancy.owner).post(url(tenancy, "create/"), {"name": "  "})

        assert response.status_code == 204
        assert triggers(response)["showToast"]["tone"] == "error"
        assert "sequencesChanged" not in triggers(response)

    def test_a_duplicate_name_is_refused(self, tenancy, client_for):
        Sequence.objects.create(workspace=tenancy.workspace, name="Onboarding")

        response = client_for(tenancy.owner).post(url(tenancy, "create/"), {"name": "onboarding"})

        assert triggers(response)["showToast"]["tone"] == "error"
        assert Sequence.objects.for_workspace(tenancy.workspace).count() == 1

    def test_activating_a_sequence_with_no_steps_is_refused(self, tenancy, client_for):
        sequence = Sequence.objects.create(workspace=tenancy.workspace, name="Empty")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/status/"), {"status": SequenceStatus.ACTIVE}
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        sequence.refresh_from_db()
        assert sequence.status == SequenceStatus.DRAFT

    def test_deleting_one_cancels_its_queued_steps(self, tenancy, client_for):
        from apps.queueing.models import ActionStatus, ActionType, ScheduledAction

        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace))

        client_for(tenancy.owner).post(url(tenancy, f"{sequence.pk}/delete/"))

        assert not Sequence.objects.for_workspace(tenancy.workspace).exists()
        rows = ScheduledAction.objects.for_workspace(tenancy.workspace).filter(type=ActionType.SEQUENCE_STEP)
        assert rows.filter(status=ActionStatus.PENDING).count() == 0


@pytest.mark.django_db
class TestSteps:
    def test_adding_one_stores_the_delay_and_the_window(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=0)
        flow = runnable_flow(tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/steps/create/"),
            {
                "flow_id": str(flow.pk),
                "delay_value": "3",
                "delay_unit": "hours",
                "window_enabled": "on",
                "window_days": ["mon", "not-a-day"],
                "window_from": "09:00",
                "window_to": "17:00",
                "window_contact_tz": "on",
            },
        )

        assert triggers(response)["sequenceStepsChanged"] is True
        step = SequenceStep.objects.for_workspace(tenancy.workspace).get()
        assert (step.position, step.delay_value, step.delay_unit) == (1, 3, "hours")
        # The allowlist drops a weekday nothing recognises rather than storing it.
        assert step.window["days"] == ["mon"]
        assert step.window["use_contact_timezone"] is True

    def test_a_flow_from_another_workspace_is_a_404(self, tenancy, other_tenancy, client_for):
        """The id arrives in the body, where tests/idor.py cannot reach it."""
        sequence = sequence_with(tenancy.workspace, steps=0)
        theirs = runnable_flow(other_tenancy.workspace)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/steps/create/"),
            {"flow_id": str(theirs.pk), "delay_value": "1", "delay_unit": "days"},
        )

        assert response.status_code == 404
        assert not SequenceStep.objects.for_workspace(tenancy.workspace).exists()

    def test_a_step_of_another_sequence_is_a_404(self, tenancy, client_for):
        """Both ids are in the URL, so the sweep reaches the tenancy half — this
        is the pairing it cannot check: a real step of the *wrong* sequence."""
        mine = sequence_with(tenancy.workspace, steps=1, name="Mine")
        other = sequence_with(tenancy.workspace, steps=1, name="Other")
        step = other.steps.get()

        response = client_for(tenancy.owner).post(url(tenancy, f"{mine.pk}/steps/{step.pk}/delete/"))

        assert response.status_code == 404

    def test_an_absurd_delay_is_refused(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=0)
        flow = runnable_flow(tenancy.workspace, name="Welcome")

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/steps/create/"),
            {"flow_id": str(flow.pk), "delay_value": "999999", "delay_unit": "days"},
        )

        assert triggers(response)["showToast"]["tone"] == "error"
        assert not SequenceStep.objects.for_workspace(tenancy.workspace).exists()

    def test_the_panel_shows_how_many_are_waiting_on_each_step(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace, first_name="A"))
        services.subscribe(sequence, contact_for(tenancy.workspace, first_name="B"))

        steps = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/steps/")).context["steps"]

        assert [step.waiting_count for step in steps] == [2, 0]


@pytest.mark.django_db
class TestSubscribers:
    def test_adding_one_enrolls_them(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/subscribers/add/"), {"contact_id": str(contact.pk)}
        )

        assert triggers(response)["sequenceSubscribersChanged"] is True
        assert SequenceEnrollment.objects.for_workspace(tenancy.workspace).get().contact_id == contact.pk

    def test_another_workspace_s_contact_is_a_404(self, tenancy, other_tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=1)
        theirs = contact_for(other_tenancy.workspace)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/subscribers/add/"), {"contact_id": str(theirs.pk)}
        )

        assert response.status_code == 404
        assert not SequenceEnrollment.objects.for_workspace(tenancy.workspace).exists()

    def test_the_typeahead_finds_a_contact_by_name(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact_for(tenancy.workspace, first_name="Grace", last_name="Hopper")
        contact_for(tenancy.workspace, first_name="Ada", last_name="Lovelace")

        body = (
            client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/suggest/?q=hopp")).content.decode()
        )

        assert "Grace Hopper" in body
        assert "Ada Lovelace" not in body

    def test_the_typeahead_omits_people_already_on_the_sequence(self, tenancy, client_for):
        """Offering them would only restart them, which is a deliberate act and
        belongs in the CRM's bulk control."""
        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace, first_name="Grace"))

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/suggest/")).content.decode()

        assert "Grace" not in body
        assert "Everyone is already on this sequence" in body

    def test_the_typeahead_omits_soft_deleted_contacts(self, tenancy, client_for):
        from apps.contacts import services as contact_services

        sequence = sequence_with(tenancy.workspace, steps=1)
        contact_services.delete_contact(contact_for(tenancy.workspace, first_name="Grace"))

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/suggest/")).content.decode()

        assert "Grace" not in body

    @pytest.mark.parametrize("role", READ_ONLY_ROLES)
    def test_a_reader_is_not_handed_a_contact_search(self, tenancy, client_for, role):
        sequence = sequence_with(tenancy.workspace, steps=1)

        response = client_for(tenancy.user_for(role)).get(url(tenancy, f"{sequence.pk}/subscribers/suggest/"))

        assert response.status_code == 403

    def test_a_soft_deleted_contact_cannot_be_enrolled(self, tenancy, client_for):
        """Otherwise a tombstone goes back into a send path."""
        from apps.contacts import services as contact_services

        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace)
        contact_services.delete_contact(contact)

        response = client_for(tenancy.owner).post(
            url(tenancy, f"{sequence.pk}/subscribers/add/"), {"contact_id": str(contact.pk)}
        )

        assert response.status_code == 404

    def test_removing_one_unsubscribes_them(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=2)
        enrollment = services.subscribe(sequence, contact_for(tenancy.workspace))

        client_for(tenancy.owner).post(url(tenancy, f"{sequence.pk}/subscribers/{enrollment.pk}/remove/"))

        enrollment.refresh_from_db()
        assert enrollment.status == EnrollmentStatus.UNSUBSCRIBED

    def test_an_enrollment_of_another_sequence_is_a_404(self, tenancy, client_for):
        mine = sequence_with(tenancy.workspace, steps=1, name="Mine")
        other = sequence_with(tenancy.workspace, steps=1, name="Other")
        enrollment = services.subscribe(other, contact_for(tenancy.workspace))

        response = client_for(tenancy.owner).post(url(tenancy, f"{mine.pk}/subscribers/{enrollment.pk}/remove/"))

        assert response.status_code == 404

    def test_the_panel_filters_by_status(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace)
        services.subscribe(sequence, contact)
        services.unsubscribe(sequence, contact)

        active = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/")).context["enrollments"]
        gone = (
            client_for(tenancy.owner)
            .get(url(tenancy, f"{sequence.pk}/subscribers/?status=unsubscribed"))
            .context["enrollments"]
        )

        assert active == []
        assert len(gone) == 1

    def test_a_hostile_contact_name_is_escaped(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=1)
        services.subscribe(sequence, contact_for(tenancy.workspace, first_name="<img src=x onerror=alert(1)>"))

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/")).content.decode()

        assert "<img src=x" not in body
        assert "&lt;img" in body


@pytest.mark.django_db
class TestTheNav:
    def test_the_sequences_row_points_at_the_real_page(self, tenancy, client_for):
        """Issue #22 replaced the placeholder; the nav entry is data, so only its
        ``url_name`` changed."""
        response = client_for(tenancy.owner).get(url(tenancy, ""))

        row = next(
            item for group in response.context["nav_groups"] for item in group["items"] if item["key"] == "sequences"
        )
        assert row["url"] == url(tenancy, "")
        assert row["active"] is True
