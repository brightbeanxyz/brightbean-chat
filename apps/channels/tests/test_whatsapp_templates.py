"""Template authoring, submission, review and rendering (SPEC §6.5, §15).

The lifecycle the issue asks for end to end: create -> submit (mocked Graph) ->
pending -> poll -> approved -> usable, and rejected -> reason displayed.

Two properties get more attention than the rest, because they are the ones a
security review will ask about:

* **the SSTI ban** (SECURITY-BASELINE §3). A template body is authored by an
  operator and filled with values that came from a stranger. Every substitution
  goes through ``apps.flows.rendering.render``, so template syntax in a value is
  inert text rather than something evaluated;
* **the preview is the send path's own renderer**, so what an operator approves
  and what a contact receives cannot drift.
"""

from decimal import Decimal
from typing import Any

import pytest

from apps.channels import whatsapp_templates
from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus
from apps.channels.providers.exceptions import APIError
from apps.channels.tests.whatsapp_support import Reply, fake_graph_api, make_connection
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db


def make_template(workspace: Any, connection: Any, **overrides: Any) -> WhatsAppTemplate:
    fields: dict[str, Any] = {
        "workspace": workspace,
        "channel_connection": connection,
        "name": "order_shipped",
        "language": "en_US",
        "category": "utility",
        "body_structure": {
            "header": {"format": "text", "text": "Order {{1}}"},
            "body": {"text": "Hi {{1}}, your order {{2}} is on its way."},
            "footer": {"text": "Reply STOP to opt out."},
            "buttons": [
                {"type": "quick_reply", "text": "Track"},
                {"type": "url", "text": "Open", "url": "https://shop.test/orders/{{1}}"},
            ],
        },
    }
    fields.update(overrides)
    template = WhatsAppTemplate(**fields)
    template.save()
    return template


@pytest.fixture
def connection(tenancy: Any) -> Any:
    return make_connection(tenancy.workspace)


@pytest.fixture
def template(tenancy: Any, connection: Any) -> WhatsAppTemplate:
    return make_template(tenancy.workspace, connection)


class TestSlots:
    def test_slots_are_named_per_component_and_sorted_numerically(self, template: WhatsAppTemplate) -> None:
        """Meta numbers per component: a header's {{1}} and a body's {{1}} are
        two different parameters."""
        assert whatsapp_templates.slots_for(template) == ("header.1", "body.1", "body.2", "button.1.1")

    def test_a_repeated_placeholder_asks_for_one_value(self, tenancy: Any, connection: Any) -> None:
        template = make_template(
            tenancy.workspace,
            connection,
            name="repeat",
            body_structure={"body": {"text": "{{1}} and {{1}} again"}},
        )
        assert whatsapp_templates.slots_for(template) == ("body.1",)

    def test_double_digit_slots_sort_by_number_not_by_string(self, tenancy: Any, connection: Any) -> None:
        template = make_template(
            tenancy.workspace,
            connection,
            name="many",
            body_structure={"body": {"text": " ".join(f"{{{{{n}}}}}" for n in (10, 2, 1))}},
        )
        assert whatsapp_templates.slots_for(template) == ("body.1", "body.2", "body.10")

    def test_a_named_placeholder_is_not_a_slot(self, tenancy: Any, connection: Any) -> None:
        """In a template {{first_name}} is literal text Meta will show verbatim.

        Reporting it as a slot would ask the operator for a value that is never
        substituted.
        """
        template = make_template(
            tenancy.workspace,
            connection,
            name="named",
            body_structure={"body": {"text": "Hi {{first_name}}"}},
        )
        assert whatsapp_templates.slots_for(template) == ()

    def test_a_body_structure_of_the_wrong_shape_asks_for_nothing(self, tenancy: Any, connection: Any) -> None:
        template = make_template(tenancy.workspace, connection, name="broken", body_structure={"body": "not a dict"})
        assert whatsapp_templates.slots_for(template) == ()


class TestPreview:
    def test_it_fills_the_slots(self, template: WhatsAppTemplate) -> None:
        rendered = whatsapp_templates.preview(template, {"body.1": "Ada", "body.2": "42", "header.1": "42"})
        assert rendered["body"] == "Hi Ada, your order 42 is on its way."
        assert rendered["header"] == "Order 42"

    def test_a_footer_is_passed_through_untouched(self, template: WhatsAppTemplate) -> None:
        assert whatsapp_templates.preview(template, {})["footer"] == "Reply STOP to opt out."

    def test_an_unfilled_slot_renders_empty_rather_than_echoing_the_template(self, template: WhatsAppTemplate) -> None:
        assert whatsapp_templates.preview(template, {})["body"] == "Hi , your order  is on its way."

    @pytest.mark.parametrize(
        "hostile",
        [
            "{{body.2}}",
            "{{2}}",
            "{% load foo %}",
            "${7*7}",
            "#{7*7}",
            "{{ settings.SECRET_KEY }}",
        ],
    )
    def test_a_value_is_never_rescanned_or_evaluated(self, template: WhatsAppTemplate, hostile: str) -> None:
        """SECURITY-BASELINE §3. One re.sub pass with a replacement callable: the
        callable's return value is never looked at again, so template syntax in a
        value is inert text."""
        rendered = whatsapp_templates.preview(template, {"body.1": hostile, "body.2": "42"})
        assert hostile in rendered["body"]
        assert "49" not in rendered["body"]

    def test_a_named_placeholder_is_shown_as_itself(self, tenancy: Any, connection: Any) -> None:
        """Meta substitutes {{1}}-style placeholders and nothing else, so
        {{first_name}} in an approved body is literal text it renders verbatim.

        The shared renderer's grammar is wider, and left alone it resolved the
        token against an empty context and deleted it — the operator approved
        "Hi ," and the contact received "Hi {{first_name}}", which is exactly
        the drift the preview exists to prevent.
        """
        template = make_template(
            tenancy.workspace,
            connection,
            name="named_token",
            body_structure={"body": {"text": "Hi {{first_name}}, order {{1}} shipped"}},
        )
        assert whatsapp_templates.preview(template, {"body.1": "42"})["body"] == ("Hi {{first_name}}, order 42 shipped")

    def test_a_named_placeholder_is_still_not_a_slot(self, tenancy: Any, connection: Any) -> None:
        """Showing it verbatim must not turn it into a value to ask the operator
        for — it is text, not a parameter."""
        template = make_template(
            tenancy.workspace,
            connection,
            name="named_only",
            body_structure={"body": {"text": "Hi {{first_name}}"}},
        )
        assert whatsapp_templates.slots_for(template) == ()

    def test_a_supplied_value_still_wins_over_the_literal(self, tenancy: Any, connection: Any) -> None:
        template = make_template(
            tenancy.workspace,
            connection,
            name="both_kinds",
            body_structure={"body": {"text": "{{first_name}} {{1}}"}},
        )
        assert whatsapp_templates.preview(template, {"body.1": "X"})["body"] == "{{first_name}} X"

    def test_a_slot_belongs_to_its_own_component(self, template: WhatsAppTemplate) -> None:
        """header.1 must not leak into the body's {{1}}."""
        rendered = whatsapp_templates.preview(template, {"header.1": "HEADER", "body.1": "BODY"})
        assert "HEADER" not in rendered["body"]
        assert rendered["header"] == "Order HEADER"


class TestSubmissionPayload:
    def test_components_are_metas_shape(self, template: WhatsAppTemplate) -> None:
        components = whatsapp_templates.submission_components(template)
        assert [c["type"] for c in components] == ["HEADER", "BODY", "FOOTER", "BUTTONS"]
        assert components[1]["text"].startswith("Hi {{1}}")

    def test_examples_are_generated_because_meta_refuses_a_template_without_them(
        self, template: WhatsAppTemplate
    ) -> None:
        """An operator has no way to know that from the form."""
        header, body, _footer, buttons = whatsapp_templates.submission_components(template)
        assert header["example"] == {"header_text": ["sample 1"]}
        assert body["example"] == {"body_text": [["sample 1", "sample 2"]]}
        assert buttons["buttons"][1]["example"] == ["https://shop.test/orders/example"]

    def test_a_template_with_no_variables_carries_no_example(self, tenancy: Any, connection: Any) -> None:
        template = make_template(
            tenancy.workspace, connection, name="plain", body_structure={"body": {"text": "Thanks!"}}
        )
        (body,) = whatsapp_templates.submission_components(template)
        assert "example" not in body

    def test_slot_indices_match_metas_button_indices(self, tenancy: Any, connection: Any) -> None:
        """A skipped button must not shift one numbering and not the other.

        `slots_for` names a slot after its button's index and the adapter sends
        that index to Meta, so walking the raw list here while the submission
        builder skipped unusable buttons produced `index` values pointing at
        buttons Meta does not have — a template that looks approved and fails
        every send.
        """
        template = make_template(
            tenancy.workspace,
            connection,
            name="skipped_button",
            body_structure={
                "body": {"text": "hi"},
                "buttons": [
                    {"type": "quick_reply", "text": ""},
                    {"type": "url", "text": "Open", "url": "https://x.test/{{1}}"},
                ],
            },
        )
        assert whatsapp_templates.slots_for(template) == ("button.0.1",)
        (buttons,) = [c for c in whatsapp_templates.submission_components(template) if c["type"] == "BUTTONS"]
        assert [b["type"] for b in buttons["buttons"]] == ["URL"]

    def test_quick_reply_and_url_buttons_use_metas_own_names(self, template: WhatsAppTemplate) -> None:
        buttons = whatsapp_templates.submission_components(template)[3]["buttons"]
        assert [b["type"] for b in buttons] == ["QUICK_REPLY", "URL"]


class TestSubmit:
    def test_it_moves_a_draft_to_pending_and_records_metas_id(self, template: WhatsAppTemplate) -> None:
        with fake_graph_api() as fake:
            fake.reply("message_templates", Reply(body={"id": "META_TPL_1", "status": "PENDING"}))
            whatsapp_templates.submit(template)

        template.refresh_from_db()
        assert template.status == WhatsAppTemplateStatus.PENDING
        assert template.meta_template_id == "META_TPL_1"
        assert fake.paths() == ["/v21.0/102290129340398/message_templates"]

    def test_a_refusal_leaves_the_draft_alone(self, template: WhatsAppTemplate) -> None:
        """A template stuck in a review that never started is worse than a draft."""
        with fake_graph_api() as fake:
            fake.reply("message_templates", Reply(status=400))
            with pytest.raises(APIError):
                whatsapp_templates.submit(template)

        template.refresh_from_db()
        assert template.status == WhatsAppTemplateStatus.DRAFT
        assert template.meta_template_id == ""

    def test_a_name_meta_would_reject_never_reaches_the_network(self, tenancy: Any, connection: Any) -> None:
        template = make_template(tenancy.workspace, connection, name="Order Shipped")
        with fake_graph_api() as fake, pytest.raises(APIError, match="lowercase"):
            whatsapp_templates.submit(template)
        assert fake.calls == []

    def test_a_connection_with_no_waba_is_refused_before_the_call(self, tenancy: Any, connection: Any) -> None:
        from apps.channels.providers import whatsapp

        whatsapp.store_credentials(connection, token="x", waba_id="", phone_number_id="1")
        connection.save(update_fields=["credentials", "updated_at"])
        template = make_template(tenancy.workspace, connection, name="no_waba")
        with fake_graph_api() as fake, pytest.raises(APIError, match="Business Account"):
            whatsapp_templates.submit(template)
        assert fake.calls == []

    def test_resubmitting_a_rejected_template_clears_the_old_verdict(self, tenancy: Any, connection: Any) -> None:
        template = make_template(
            tenancy.workspace,
            connection,
            name="second_try",
            status=WhatsAppTemplateStatus.REJECTED,
            rejected_reason="INVALID_FORMAT",
        )
        with fake_graph_api() as fake:
            fake.reply("message_templates", Reply(body={"id": "META_TPL_2"}))
            whatsapp_templates.submit(template)
        template.refresh_from_db()
        assert template.rejected_reason == ""


class TestResetToDraft:
    def test_it_clears_the_verdict_and_the_meta_id(self, tenancy: Any, connection: Any) -> None:
        """A stale meta_template_id would keep the poll asking Meta about a
        template that no longer matches what is stored here."""
        template = make_template(
            tenancy.workspace,
            connection,
            name="was_rejected",
            status=WhatsAppTemplateStatus.REJECTED,
            rejected_reason="INVALID_FORMAT",
            meta_template_id="META_TPL_1",
        )
        whatsapp_templates.reset_to_draft(template)

        assert template.status == WhatsAppTemplateStatus.DRAFT
        assert template.rejected_reason == ""
        assert template.meta_template_id == ""

    def test_it_does_not_save(self, tenancy: Any, connection: Any) -> None:
        """The caller is mid-``form.save(commit=False)`` and owns the write."""
        template = make_template(tenancy.workspace, connection, status=WhatsAppTemplateStatus.REJECTED)
        whatsapp_templates.reset_to_draft(template)
        template.refresh_from_db()
        assert template.status == WhatsAppTemplateStatus.REJECTED


class TestPolling:
    @pytest.fixture
    def pending(self, tenancy: Any, connection: Any) -> WhatsAppTemplate:
        return make_template(
            tenancy.workspace,
            connection,
            status=WhatsAppTemplateStatus.PENDING,
            meta_template_id="META_TPL_1",
        )

    def test_approved_becomes_usable(self, pending: WhatsAppTemplate) -> None:
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "APPROVED"}))
            assert whatsapp_templates.poll_pending() is not None

        pending.refresh_from_db()
        assert pending.status == WhatsAppTemplateStatus.APPROVED
        assert pending.is_usable

    def test_rejected_keeps_metas_reason(self, pending: WhatsAppTemplate) -> None:
        with fake_graph_api() as fake:
            fake.reply(
                "META_TPL_1",
                Reply(body={"id": "META_TPL_1", "status": "REJECTED", "rejected_reason": "INVALID_FORMAT"}),
            )
            whatsapp_templates.poll_pending()

        pending.refresh_from_db()
        assert pending.status == WhatsAppTemplateStatus.REJECTED
        assert pending.rejected_reason == "INVALID_FORMAT"

    def test_a_paused_template_is_not_sendable_and_says_why(self, pending: WhatsAppTemplate) -> None:
        """PAUSED is not a rejection in Meta's sense, but it is not sendable
        either, and failing closed is the only safe direction for a gate a
        compliance rule depends on."""
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "PAUSED", "rejected_reason": "NONE"}))
            whatsapp_templates.poll_pending()

        pending.refresh_from_db()
        assert not pending.is_usable
        assert pending.rejected_reason == "PAUSED"

    def test_still_pending_stays_pending_and_reports_nothing(self, pending: WhatsAppTemplate) -> None:
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "PENDING"}))
            assert whatsapp_templates.poll_pending() is None

        pending.refresh_from_db()
        assert pending.status == WhatsAppTemplateStatus.PENDING

    def test_an_unrecognised_status_leaves_the_row_alone(self, pending: WhatsAppTemplate) -> None:
        """Meta adding a state must not make a template sendable by accident."""
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "SOMETHING_NEW"}))
            whatsapp_templates.poll_pending()

        pending.refresh_from_db()
        assert pending.status == WhatsAppTemplateStatus.PENDING

    def test_one_unreachable_template_does_not_stop_the_others(
        self, tenancy: Any, connection: Any, pending: WhatsAppTemplate
    ) -> None:
        """One workspace's revoked token is not a reason to stop polling the rest."""
        second = make_template(
            tenancy.workspace,
            connection,
            name="other_one",
            status=WhatsAppTemplateStatus.PENDING,
            meta_template_id="META_TPL_2",
        )
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(status=401))
            fake.reply("META_TPL_2", Reply(body={"id": "META_TPL_2", "status": "APPROVED"}))
            summary = whatsapp_templates.poll_pending()

        second.refresh_from_db()
        assert second.status == WhatsAppTemplateStatus.APPROVED
        assert "1 unreachable" in (summary or "")

    def test_a_template_never_submitted_is_not_polled(self, tenancy: Any, connection: Any) -> None:
        make_template(tenancy.workspace, connection, name="never_sent", status=WhatsAppTemplateStatus.PENDING)
        with fake_graph_api() as fake:
            whatsapp_templates.poll_pending()
        assert fake.calls == []

    def test_it_is_idempotent(self, pending: WhatsAppTemplate) -> None:
        """The hourly sweep re-runs everything when any job fails."""
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "APPROVED"}))
            whatsapp_templates.poll_pending()
            assert whatsapp_templates.poll_pending() is None

    def test_a_review_outcome_notifies_workspace_admins(self, pending: WhatsAppTemplate, tenancy: Any) -> None:
        """L2-E registered ``whatsapp_template_reviewed`` naming this issue as
        its consumer, so the copy already exists and this supplies the context."""
        with fake_graph_api() as fake:
            fake.reply("META_TPL_1", Reply(body={"id": "META_TPL_1", "status": "APPROVED"}))
            whatsapp_templates.poll_pending()

        notification = Notification.objects.filter(event_type="whatsapp_template_reviewed").first()
        assert notification is not None
        assert "order_shipped" in notification.title
        assert notification.user_id == tenancy.owner.pk


class TestHousekeepingRegistration:
    def test_the_hourly_sweep_finds_the_poll(self) -> None:
        """``OPTIONAL_JOB_PATHS`` reserved the name and the dotted path for #19
        before either module existed; keeping the path pointing at something real
        is what registers the job with no registration line anywhere."""
        from apps.queueing.housekeeping import housekeeping_jobs

        assert "poll_whatsapp_templates" in housekeeping_jobs()

    def test_the_reserved_path_resolves_to_the_service(self) -> None:
        from apps.channels.providers import whatsapp

        with fake_graph_api():
            assert whatsapp.poll_template_statuses() is None


class TestDelete:
    def test_it_deletes_at_meta_first(self, tenancy: Any, connection: Any) -> None:
        """A local row removed while Meta still holds the template leaves a name
        that can never be reused."""
        template = make_template(tenancy.workspace, connection, meta_template_id="META_TPL_1")
        with fake_graph_api() as fake:
            whatsapp_templates.delete_template(template)

        assert fake.calls[0][0] == "DELETE"
        assert fake.queries[0]["hsm_id"] == "META_TPL_1"
        assert not WhatsAppTemplate.objects.for_workspace(tenancy.workspace).exists()

    def test_a_failure_at_meta_still_removes_the_local_row(self, tenancy: Any, connection: Any) -> None:
        """An operator who cannot delete a template has no other way to fix it."""
        template = make_template(tenancy.workspace, connection, meta_template_id="META_TPL_1")
        with fake_graph_api() as fake:
            fake.reply("message_templates", Reply(status=500))
            whatsapp_templates.delete_template(template)
        assert not WhatsAppTemplate.objects.for_workspace(tenancy.workspace).exists()

    def test_a_draft_never_submitted_needs_no_call(self, tenancy: Any, connection: Any) -> None:
        template = make_template(tenancy.workspace, connection)
        with fake_graph_api() as fake:
            whatsapp_templates.delete_template(template)
        assert fake.calls == []


class TestSelectors:
    def test_only_approved_templates_are_offered(self, tenancy: Any, connection: Any) -> None:
        approved = make_template(tenancy.workspace, connection, name="ready", status=WhatsAppTemplateStatus.APPROVED)
        make_template(tenancy.workspace, connection, name="waiting", status=WhatsAppTemplateStatus.PENDING)
        assert whatsapp_templates.approved_templates_for(tenancy.workspace) == [approved]

    def test_another_workspaces_templates_are_invisible(
        self, tenancy: Any, other_tenancy: Any, connection: Any
    ) -> None:
        make_template(tenancy.workspace, connection, name="mine", status=WhatsAppTemplateStatus.APPROVED)
        assert whatsapp_templates.approved_templates_for(other_tenancy.workspace) == []

    def test_the_variable_schema_is_what_a_composer_needs(self, template: WhatsAppTemplate) -> None:
        schema = whatsapp_templates.variable_schema(template)
        assert schema["reference"] == "order_shipped/en_US"
        assert schema["slots"] == ["header.1", "body.1", "body.2", "button.1.1"]
        assert schema["category"] == "utility"


class TestCostHints:
    def test_reading_never_writes(self, tenancy: Any) -> None:
        """A settings page that created a row on first view would INSERT on a GET."""
        from apps.channels.models import WhatsAppCostHint

        hint = whatsapp_templates.cost_hint_for(tenancy.workspace)
        # `pk` is already set on an unsaved row — BaseModel generates a UUIDv7 as
        # the field default — so "unsaved" is `_state.adding`, not a null pk.
        assert hint._state.adding
        assert not WhatsAppCostHint.objects.for_workspace(tenancy.workspace).exists()

    def test_the_default_is_zero_rather_than_a_guess(self, tenancy: Any) -> None:
        """A made-up price shown as a hint is worse than an absent one."""
        assert whatsapp_templates.cost_hint_for(tenancy.workspace).amount_for("marketing") == Decimal("0")

    def test_saving_then_reading_round_trips(self, tenancy: Any) -> None:
        whatsapp_templates.save_cost_hint(
            tenancy.workspace,
            currency="EUR",
            amounts={"marketing": Decimal("0.0512"), "utility": Decimal("0.01"), "authentication": Decimal("0.02")},
        )
        hint = whatsapp_templates.cost_hint_for(tenancy.workspace)
        assert hint.currency == "EUR"
        assert hint.amount_for("utility") == Decimal("0.0100")

    def test_saving_twice_updates_rather_than_duplicating(self, tenancy: Any) -> None:
        from apps.channels.models import WhatsAppCostHint

        for amount in ("0.01", "0.02"):
            whatsapp_templates.save_cost_hint(
                tenancy.workspace,
                currency="USD",
                amounts={"marketing": Decimal(amount), "utility": Decimal("0"), "authentication": Decimal("0")},
            )
        assert WhatsAppCostHint.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_category_that_is_not_one_answers_the_default(self, tenancy: Any) -> None:
        """The field names are the category values, so an unconstrained getattr
        would happily return `currency`."""
        hint = whatsapp_templates.cost_hint_for(tenancy.workspace)
        assert hint.amount_for("currency") == Decimal("0")
