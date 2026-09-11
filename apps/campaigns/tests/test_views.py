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

        response = client_for(tenancy.user_for(role)).get(url(tenancy, f"{sequence.pk}/"))

        assert "Add a step" not in response.content.decode()
        # And does not pay for the picker behind them: `_step_fields.html` is the
        # only consumer and it is included solely under `{% if can_edit %}`.
        assert response.context["flow_options"] == []


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

    def test_an_unrecognised_status_falls_back_to_the_unfiltered_view(self, tenancy, client_for):
        """Sanitised before the query, not only for the template. Validating it
        only on the way out left `?status=bogus` returning an empty list under
        the "create your first one" empty state, with no visible filter to
        clear — the same correction `apps/flows/views.py` documents."""
        sequence_with(tenancy.workspace, steps=1, name="Onboarding")

        response = client_for(tenancy.owner).get(url(tenancy, "?status=bogus"))

        assert [row.name for row in response.context["sequences"]] == ["Onboarding"]
        assert response.context["status"] == ""

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

    def test_a_one_unit_delay_reads_as_one_unit(self, tenancy, client_for):
        """DelayUnit's labels are plural because they name the unit in a
        <select>, where there is no number beside them. Reused next to a value,
        they produced "Wait 1 minutes" on every step anyone set to one."""
        sequence = sequence_with(tenancy.workspace, steps=1, delay_value=1, delay_unit="minutes")

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/")).content.decode()

        assert "Wait 1 minute" in body
        assert "1 minutes" not in body

    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (1, "minutes", "1 minute"),
            (2, "minutes", "2 minutes"),
            (1, "hours", "1 hour"),
            (3, "hours", "3 hours"),
            (1, "days", "1 day"),
            (7, "days", "7 days"),
        ],
    )
    def test_every_unit_and_count_reads_as_a_person_would_say_it(self, tenancy, value, unit, expected):
        sequence = sequence_with(tenancy.workspace, steps=1, delay_value=value, delay_unit=unit)

        assert sequence.steps.first().delay_label == expected

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

    def test_a_finished_subscriber_is_not_parked_on_a_step_nobody_built(self, tenancy, client_for):
        """current_step is the position of the step that runs *next*, so a
        finished enrollment deliberately sits one past the end. Rendered raw,
        that told anyone who completed a one-step sequence they were on
        "Step 2"."""
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace, first_name="Ada")
        services.subscribe(sequence, contact)
        enrollment = SequenceEnrollment.objects.for_workspace(tenancy.workspace.pk).get(contact=contact)
        enrollment.current_step = 2
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.save()

        body = (
            client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/?status=completed")).content.decode()
        )

        assert "Step 2" not in body
        assert "Finished" in body

    def test_an_empty_filter_says_the_view_is_empty_not_the_sequence(self, tenancy, client_for):
        """With the filter on Active and one completed subscriber present, the
        panel claimed "Nobody here yet." — which the Completed tab immediately
        contradicted."""
        sequence = sequence_with(tenancy.workspace, steps=1)
        contact = contact_for(tenancy.workspace, first_name="Ada")
        services.subscribe(sequence, contact)
        enrollment = SequenceEnrollment.objects.for_workspace(tenancy.workspace.pk).get(contact=contact)
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.save()

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/")).content.decode()

        assert "Nobody here yet" not in body
        assert "on this sequence in all" in body

    def test_a_sequence_with_nobody_on_it_still_says_so_plainly(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=1)

        body = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/")).content.decode()

        assert "Nobody here yet" in body

    def test_the_panel_says_when_it_has_truncated(self, tenancy, client_for, monkeypatch):
        """A list that silently stops beside a count in the thousands is two
        numbers disagreeing, and somebody scanning for one person concludes they
        are not enrolled."""
        from apps.campaigns import selectors

        monkeypatch.setattr(selectors, "MAX_SUBSCRIBERS", 2)
        sequence = sequence_with(tenancy.workspace, steps=2)
        for index in range(4):
            services.subscribe(sequence, contact_for(tenancy.workspace, first_name=f"C{index}"))

        response = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/"))

        assert response.context["truncated"] is True
        assert response.context["subscriber_total"] == 4
        assert len(response.context["enrollments"]) == 2
        assert "Showing the 2 most recent of 4" in response.content.decode()

    def test_an_untruncated_panel_just_reports_the_count(self, tenancy, client_for):
        sequence = sequence_with(tenancy.workspace, steps=2)
        services.subscribe(sequence, contact_for(tenancy.workspace))

        response = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/"))

        assert response.context["truncated"] is False
        assert "most recent of" not in response.content.decode()

    def test_a_deleted_contact_drops_out_of_the_panel(self, tenancy, client_for):
        from apps.contacts import services as contact_services

        sequence = sequence_with(tenancy.workspace, steps=2)
        contact = contact_for(tenancy.workspace, first_name="Gone")
        services.subscribe(sequence, contact)
        contact_services.delete_contact(contact)

        response = client_for(tenancy.owner).get(url(tenancy, f"{sequence.pk}/subscribers/"))

        assert response.context["enrollments"] == []
        assert "Gone" not in response.content.decode()

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
