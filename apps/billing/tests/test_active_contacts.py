"""The contact meter: who counts, when, and what it refuses.

The properties worth having here are mostly *negative* — what does not get
counted — because every one of them is a way a free tier either strangles a user
who is inside their allowance or leaks to somebody outside it.

The one that is not negotiable is
``test_a_compliance_reply_is_never_refused``. SPEC §6.6 requires an SMS ``STOP``
to be answered. A carrier obligation is not a message anybody chose to send, and
refusing it because a card expired would be a compliance failure dressed up as a
billing decision. It lives here rather than in ``apps/messaging/tests`` only
because everything it needs is here; the structural half — that
``send_compliance_reply`` is a separate function which never calls
``send_outbound`` — is asserted below too, because that, not a flag, is what
makes the exemption true.
"""

from typing import Any

import pytest

from apps.billing.metering import (
    MarkOutcome,
    active_contact_count,
    current_period,
    is_contact_active,
    mark_reached,
    meter,
    reach,
)
from apps.billing.models import ActiveContactMonth
from apps.billing.plans import FREE_LIMITS
from apps.billing.tests.stripe_support import SECRET_KEY

pytestmark = pytest.mark.django_db

#: The free plan's caps, narrowed once for the type checker.
#:
#: ``Limits`` types every cap ``int | None`` because ``None`` means unlimited —
#: which is true of the paid plan and never of the free one. A ``None`` here
#: would mean the free plan had stopped capping that dimension, so asserting it
#: at import is both the narrowing and a check on the constant.
assert FREE_LIMITS.channels is not None
assert FREE_LIMITS.active_contacts_per_month is not None
assert FREE_LIMITS.seats is not None
FREE_CHANNELS: int = FREE_LIMITS.channels
FREE_CONTACTS: int = FREE_LIMITS.active_contacts_per_month
FREE_SEATS: int = FREE_LIMITS.seats


@pytest.fixture(autouse=True)
def _billing_on(settings: Any) -> None:
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = SECRET_KEY
    settings.STRIPE_PRICE_ID_MONTHLY = "price_m"
    settings.STRIPE_PRICE_ID_YEARLY = "price_y"


def contact(workspace: Any, name: str) -> Any:
    from apps.contacts.models import Contact

    return Contact.objects.create(workspace=workspace, first_name=name)


def fill_to_limit(tenancy: Any) -> list[Any]:
    """Reach the month's whole allowance, gate then mark, as a send does."""
    people = [contact(tenancy.workspace, f"person {i}") for i in range(FREE_CONTACTS)]
    for person in people:
        assert reach(tenancy.workspace, person) is True
        mark_reached(tenancy.organization, person)
    return people


class TestItIsASetNotACounter:
    def test_the_same_contact_many_times_is_one_row(self, tenancy: Any) -> None:
        """The property the whole design rests on: a set has no decrement, so it
        cannot drift the way apps/media_library/quotas.py warns a counter does."""
        person = contact(tenancy.workspace, "Ada")

        for _ in range(10):
            mark_reached(tenancy.organization, person)

        assert ActiveContactMonth.objects.unscoped().filter(contact=person).count() == 1
        assert active_contact_count(tenancy.organization, period=current_period(tenancy.organization)) == 1

    def test_deleting_a_contact_frees_their_slot(self, tenancy: Any) -> None:
        """Not incidental: this is the "remove extras" half of the plan's
        semantics, and it comes free from the cascade."""
        people = fill_to_limit(tenancy)
        newcomer = contact(tenancy.workspace, "Grace")
        assert reach(tenancy.workspace, newcomer) is False

        people[0].delete()

        assert reach(tenancy.workspace, newcomer) is True

    def test_the_row_carries_both_scoping_columns(self, tenancy: Any) -> None:
        """The raw INSERT never reaches Model.save(), so a derivation only save()
        performs would be a column that is right in tests and null in
        production."""
        person = contact(tenancy.workspace, "Ada")
        mark_reached(tenancy.organization, person)

        row = ActiveContactMonth.objects.unscoped().get()
        assert row.workspace_id == tenancy.workspace.pk
        assert row.organization_id == tenancy.organization.pk


class TestTheLimit:
    def test_a_contact_already_counted_this_month_always_passes(self, tenancy: Any) -> None:
        """An in-flight conversation is never cut off half way through, and a
        refusal stays explainable: "you have talked to 25 people this month"."""
        people = fill_to_limit(tenancy)

        assert reach(tenancy.workspace, people[0]) is True
        assert reach(tenancy.workspace, people[-1]) is True

    def test_a_new_contact_past_the_limit_is_refused(self, tenancy: Any) -> None:
        fill_to_limit(tenancy)

        assert reach(tenancy.workspace, contact(tenancy.workspace, "Grace")) is False

    def test_a_refusal_writes_no_row(self, tenancy: Any) -> None:
        fill_to_limit(tenancy)
        newcomer = contact(tenancy.workspace, "Grace")

        assert reach(tenancy.workspace, newcomer) is False

        assert is_contact_active(newcomer, period=current_period(tenancy.organization)) is False

    def test_a_paid_organization_is_never_metered(self, tenancy: Any) -> None:
        """No row at all on an unlimited plan — a performance property and a "we
        are not instrumenting you" one."""
        from apps.billing.models import BillingCustomer

        BillingCustomer.objects.create(
            organization=tenancy.organization, stripe_customer_id="cus_paid", status="active"
        )

        for index in range(FREE_CONTACTS + 5):
            person = contact(tenancy.workspace, f"p{index}")
            assert reach(tenancy.workspace, person) is True
            # NOT_METERED, not FAILED: there is nothing to count on an unlimited
            # plan, which is a different answer from having failed to count.
            assert mark_reached(tenancy.organization, person) is MarkOutcome.NOT_METERED

        assert ActiveContactMonth.objects.unscoped().count() == 0


class TestThePeriod:
    def test_it_is_the_calendar_month_in_the_organizations_timezone(self, tenancy: Any) -> None:
        tenancy.organization.default_timezone = "Pacific/Auckland"
        tenancy.organization.save(update_fields=["default_timezone"])

        assert len(current_period(tenancy.organization)) == 7
        assert current_period(tenancy.organization)[4] == "-"

    def test_an_unusable_timezone_falls_back_to_utc(self, tenancy: Any) -> None:
        """A stored timezone is a free-text column, and a meter that raises on
        one would take the send path down with it."""
        tenancy.organization.default_timezone = "Not/AZone"
        tenancy.organization.save(update_fields=["default_timezone"])

        assert current_period(tenancy.organization)

    def test_a_contact_counted_last_month_does_not_occupy_this_months_slot(self, tenancy: Any) -> None:
        person = contact(tenancy.workspace, "Ada")
        meter(tenancy.organization, person)

        ActiveContactMonth.objects.unscoped().filter(contact=person).update(period="2020-01")

        assert active_contact_count(tenancy.organization, period=current_period(tenancy.organization)) == 0


class TestTheSendPath:
    def test_an_over_limit_send_fails_with_the_plan_code_and_never_raises(self, tenancy: Any) -> None:
        """Same shape as every compliance refusal, so the flow engine follows its
        `default` edge with no new branch anywhere."""
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging import services
        from apps.messaging.codes import Limit
        from apps.messaging.models import MessageStatus
        from tests.support import email_identity

        connection = _email_connection(tenancy)
        fill_to_limit(tenancy)

        identity = email_identity(tenancy.workspace, connection, "newcomer@example.test")
        message = services.send_outbound(
            workspace=tenancy.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=OutboundMessage(blocks=(TextBlock(text="hello"),), subject="Hi"),
            source="automation",
            idempotency_key="over-limit-1",
        )

        assert message.status == MessageStatus.FAILED
        assert message.error == Limit.ACTIVE_CONTACTS.value

    def test_the_refusal_has_registered_copy(self) -> None:
        """Registered copy, not an f-string at the call site — which is what
        makes the inbox, the thread renderer and the flow-run log all render it
        with no change of their own."""
        from apps.messaging.codes import Limit, describe

        sentence = str(describe(Limit.ACTIVE_CONTACTS.value))

        assert sentence != Limit.ACTIVE_CONTACTS.value
        assert "plan" in sentence.lower()

    def test_a_compliance_reply_is_structurally_exempt(self) -> None:
        """SPEC §6.6 requires an SMS STOP to be answered.

        Asserted structurally rather than behaviourally, because the exemption
        is not a flag that could be got wrong — it is that
        ``send_compliance_reply`` is a separate function which never calls
        ``send_outbound``, so it cannot reach the gate. A refactor that routed it
        through ``send_outbound`` would be the thing that breaks this, and this
        is what would notice.
        """
        import ast
        import inspect

        from apps.messaging import services

        tree = ast.parse(inspect.getsource(services.send_compliance_reply))
        called = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "send_outbound" not in called
        assert "plan_allows_reaching" not in called


class TestAFailedSendDoesNotBurnASlot:
    """The meter marks after the platform accepts, never at the gate.

    Marking at the gate spent one of twenty-five monthly slots on a message that
    never left the building, so a workspace with a misconfigured channel could
    burn its whole allowance on provider rejections.
    """

    def test_a_provider_rejection_leaves_the_contact_uncounted(self, tenancy: Any, monkeypatch: Any) -> None:
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.messaging import services
        from apps.messaging.models import MessageStatus
        from tests.support import email_identity

        connection = _email_connection(tenancy)
        identity = email_identity(tenancy.workspace, connection, "nope@example.test")

        def refuse(*args: Any, **kwargs: Any) -> Any:
            from apps.channels.providers.base import APIError

            raise APIError("the provider said no")

        monkeypatch.setattr("apps.channels.providers.email_backends.deliver", refuse)

        message = services.send_outbound(
            workspace=tenancy.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=OutboundMessage(blocks=(TextBlock(text="hello"),), subject="Hi"),
            source="automation",
            idempotency_key="burns-nothing-1",
        )

        # A provider error schedules a retry, so the row stays QUEUED rather
        # than FAILED. Either way nothing reached anybody, which is the point.
        assert message.status in (MessageStatus.QUEUED, MessageStatus.FAILED)
        assert is_contact_active(identity.contact, period=current_period(tenancy.organization)) is False

    def test_an_accepted_send_does_count_the_contact(self, tenancy: Any, monkeypatch: Any) -> None:
        """The other half — otherwise the test above passes on a meter that
        never marks at all."""
        from apps.channels.events import OutboundMessage, TextBlock
        from apps.channels.providers import email_backends
        from apps.messaging import services
        from apps.messaging.models import MessageStatus
        from tests.support import email_identity

        connection = _email_connection(tenancy)
        identity = email_identity(tenancy.workspace, connection, "yes@example.test")
        monkeypatch.setattr(email_backends, "deliver", lambda *a, **k: "accepted-1")

        message = services.send_outbound(
            workspace=tenancy.workspace,
            contact=identity.contact,
            connection=connection,
            outbound=OutboundMessage(blocks=(TextBlock(text="hello"),), subject="Hi"),
            source="automation",
            idempotency_key="counts-1",
        )

        assert message.status == MessageStatus.SENT
        assert is_contact_active(identity.contact, period=current_period(tenancy.organization)) is True


class TestItNeverRaisesIntoTheSendPath:
    """Contract 1: ``send_outbound`` never raises. Billing does not get to.

    A raw INSERT can fail for reasons ``ON CONFLICT DO NOTHING`` does not cover —
    a contact deleted concurrently, a statement timeout — and before this the
    DatabaseError propagated out of ``send_outbound`` into every caller written
    against a promise that it would not.
    """

    def test_a_database_failure_while_marking_is_swallowed(self, tenancy: Any, monkeypatch: Any) -> None:
        from django.db import DatabaseError

        from apps.billing import metering

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise DatabaseError("the meter table is on fire")

        person = contact(tenancy.workspace, "Ada")
        # Patched after the contact exists: creating one needs a cursor too.
        monkeypatch.setattr(metering.db_connection, "cursor", explode)

        # FAILED rather than a bare False: the caller can tell a swallowed
        # database error from "there was nothing to count".
        assert metering.mark_reached(tenancy.organization, person) is metering.MarkOutcome.FAILED

    def test_a_database_failure_while_gating_allows_the_send(self, tenancy: Any, monkeypatch: Any) -> None:
        """Fails open, deliberately: a billing read must not take a send down,
        and the cost is at most one contact while the database is already in
        trouble."""
        from django.db import DatabaseError

        from apps.billing import metering

        def explode(*args: Any, **kwargs: Any) -> Any:
            raise DatabaseError("the meter table is still on fire")

        person = contact(tenancy.workspace, "Ada")
        monkeypatch.setattr(metering, "allows_reaching", explode)

        assert metering.reach(tenancy.workspace, person) is True


class TestUnconfiguredBillingTouchesNothing:
    def test_the_gate_resolves_no_organization(
        self, tenancy: Any, settings: Any, django_assert_num_queries: Any
    ) -> None:
        """`workspace.organization` is a query, and a self-hosted deployment must
        not pay it on every send."""
        settings.STRIPE_ENABLED = False
        person = contact(tenancy.workspace, "Ada")

        with django_assert_num_queries(0):
            assert reach(tenancy.workspace, person) is True


def _email_connection(tenancy: Any) -> Any:
    from apps.channels.models import ChannelConnection
    from apps.common.platforms import Platform

    row = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    row.credentials = {  # type: ignore[assignment]
        "provider": "smtp",
        "host": "mail.test",
        "security": "none",
        "from_address": "hello@sender.test",
    }
    row.save()
    return row
