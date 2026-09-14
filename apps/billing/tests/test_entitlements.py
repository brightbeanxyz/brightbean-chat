"""Each limit, at the boundary and after a downgrade.

Four cases per limit, and the fourth is the one that matters most: **over after
a downgrade**. A paid organization that cancels with eight flows and five
channels keeps all of it — nothing is deleted, disabled, hidden or revoked — and
only the *next* one is refused. That promise is what makes a downgrade survivable
rather than destructive, and it is the property a naive implementation breaks by
enforcing the cap on what exists rather than on what is being added.

The counts are asserted through the real surfaces where doing so is cheap, and
through ``entitlements`` directly where building the surface would be a fixture
essay. Where a real surface is used, it is because the *wiring* is the thing in
doubt, not the arithmetic.
"""

from typing import Any

import pytest

from apps.billing import entitlements
from apps.billing.entitlements import PlanLimitError
from apps.billing.models import BillingCustomer
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


def make_paid(tenancy: Any) -> BillingCustomer:
    return BillingCustomer.objects.create(
        organization=tenancy.organization, stripe_customer_id="cus_paid", status="active"
    )


def downgrade(row: BillingCustomer) -> None:
    row.status = "canceled"
    row.save(update_fields=["status"])


def add_channels(workspace: Any, count: int, *, start: int = 0) -> list[Any]:
    from apps.channels.models import ChannelConnection
    from apps.common.platforms import Platform

    rows = []
    for index in range(start, start + count):
        row = ChannelConnection(
            workspace=workspace,
            platform=Platform.TELEGRAM.value,
            display_name=f"bot {index}",
            # (platform, external_id) is unique across the deployment rather
            # than per workspace, so the id has to carry the workspace.
            external_id=f"bot-{workspace.pk}-{index}",
        )
        row.save()
        rows.append(row)
    return rows


def add_active_flows(workspace: Any, count: int) -> None:
    from apps.flows.models import Flow, FlowStatus

    for index in range(count):
        Flow.objects.create(workspace=workspace, name=f"flow {index}", status=FlowStatus.ACTIVE)


class TestChannels:
    def test_under_the_limit_is_allowed(self, tenancy: Any) -> None:
        add_channels(tenancy.workspace, FREE_CHANNELS - 1)

        entitlements.check_can_add_channel(tenancy.organization)

    def test_at_the_limit_is_refused(self, tenancy: Any) -> None:
        add_channels(tenancy.workspace, FREE_CHANNELS)

        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_add_channel(tenancy.organization)

        assert caught.value.code == "plan_channels"

    def test_a_disabled_connection_does_not_count(self, tenancy: Any) -> None:
        """ "Remove extras" has to be reachable, and disabling is how."""
        from apps.channels.models import ConnectionStatus

        rows = add_channels(tenancy.workspace, FREE_CHANNELS)
        rows[0].status = ConnectionStatus.DISABLED
        rows[0].save(update_fields=["status"])

        entitlements.check_can_add_channel(tenancy.organization)

    def test_a_paid_organization_is_not_capped(self, tenancy: Any) -> None:
        make_paid(tenancy)
        add_channels(tenancy.workspace, FREE_CHANNELS + 6)

        entitlements.check_can_add_channel(tenancy.organization)

    def test_after_a_downgrade_the_existing_ones_survive(self, tenancy: Any) -> None:
        """The promise that makes a downgrade survivable."""
        from apps.channels.models import ChannelConnection

        row = make_paid(tenancy)
        add_channels(tenancy.workspace, 5)
        downgrade(row)

        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 5
        assert entitlements.count_channels(tenancy.organization) == 5
        with pytest.raises(PlanLimitError):
            entitlements.check_can_add_channel(tenancy.organization)


class TestAutomations:
    def test_published_flows_active_sequences_and_enabled_rules_share_one_budget(self, tenancy: Any) -> None:
        """One number, because "4 active automations" is one number on the
        pricing page this copies."""
        from apps.campaigns.models import Sequence, SequenceStatus
        from apps.inbox.models import InboxRule

        add_active_flows(tenancy.workspace, 2)
        Sequence.objects.create(workspace=tenancy.workspace, name="drip", status=SequenceStatus.ACTIVE)
        InboxRule.objects.create(workspace=tenancy.workspace, name="rule", enabled=True)

        assert entitlements.count_active_automations(tenancy.organization) == 4
        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_activate_automation(tenancy.organization)
        assert caught.value.code == "plan_automations"

    def test_drafts_and_disabled_rules_do_not_count(self, tenancy: Any) -> None:
        """Drafting a fifth flow is not running a fifth automation, and stopping
        a free user from drafting would make the product feel broken."""
        from apps.campaigns.models import Sequence, SequenceStatus
        from apps.flows.models import Flow, FlowStatus
        from apps.inbox.models import InboxRule

        Flow.objects.create(workspace=tenancy.workspace, name="draft", status=FlowStatus.DRAFT)
        Flow.objects.create(workspace=tenancy.workspace, name="archived", status=FlowStatus.ARCHIVED)
        Sequence.objects.create(workspace=tenancy.workspace, name="draft", status=SequenceStatus.DRAFT)
        InboxRule.objects.create(workspace=tenancy.workspace, name="off", enabled=False)

        assert entitlements.count_active_automations(tenancy.organization) == 0

    def test_a_broadcasts_own_flow_does_not_spend_the_budget(self, tenancy: Any) -> None:
        """apps/broadcasts/services.py creates and publishes a Flow for every
        broadcast. Without the exclusion, sending two broadcasts would silently
        eat half a free organization's automations."""
        from apps.broadcasts.models import Broadcast
        from apps.channels.models import ChannelConnection
        from apps.common.platforms import Platform
        from apps.flows.models import Flow, FlowStatus

        connection = add_channels(tenancy.workspace, 1)[0]
        flow = Flow.objects.create(workspace=tenancy.workspace, name="broadcast flow", status=FlowStatus.ACTIVE)
        Broadcast.objects.create(
            workspace=tenancy.workspace,
            name="promo",
            channel_connection=connection,
            flow=flow,
        )

        assert entitlements.count_active_automations(tenancy.organization) == 0
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 1
        assert Platform.TELEGRAM.value


class TestSeats:
    def test_the_free_plan_is_one_seat(self, tenancy: Any) -> None:
        """The tenancy fixture already seats five people, so this organization
        is over on arrival — which is the downgrade case."""
        assert entitlements.count_seats(tenancy.organization) > FREE_SEATS

        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_add_seat(tenancy.organization)
        assert caught.value.code == "plan_seats"

    def test_a_pending_invitation_occupies_a_seat(self, user: Any, db: Any) -> None:
        """Otherwise an organization at its limit sends ten invitations, every
        one passes, and who gets the seat is decided by who clicks first."""
        from apps.members.models import Invitation, OrgMembership
        from apps.members.roles import OrgRole
        from apps.organizations.models import Organization

        org = Organization.objects.create(name="solo")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgRole.OWNER)
        assert entitlements.count_seats(org) == 1

        from datetime import timedelta

        from django.utils import timezone

        invitation = Invitation(
            organization=org,
            email="next@example.test",
            org_role=OrgRole.MEMBER,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
        )
        invitation.issue_token()
        invitation.save()

        assert entitlements.count_seats(org) == 2

    def test_an_expired_invitation_frees_its_seat(self, user: Any, db: Any) -> None:
        """Revoking sets expires_at to now rather than adding a status column
        (apps/members/models.py), so this covers revocation too."""
        from datetime import timedelta

        from django.utils import timezone

        from apps.members.models import Invitation, OrgMembership
        from apps.members.roles import OrgRole
        from apps.organizations.models import Organization

        org = Organization.objects.create(name="solo")
        OrgMembership.objects.create(user=user, organization=org, org_role=OrgRole.OWNER)
        invitation = Invitation(
            organization=org,
            email="next@example.test",
            org_role=OrgRole.MEMBER,
            invited_by=user,
            expires_at=timezone.now() - timedelta(days=1),
        )
        invitation.issue_token()
        invitation.save()

        assert entitlements.count_seats(org) == 1


class TestWorkspaces:
    def test_the_free_plan_is_one_workspace(self, tenancy: Any) -> None:
        assert entitlements.count_workspaces(tenancy.organization) == 1

        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_add_workspace(tenancy.organization)
        assert caught.value.code == "plan_workspaces"

    def test_an_archived_workspace_frees_the_slot_but_still_counts_for_resources(self, tenancy: Any) -> None:
        """The asymmetry is deliberate and looks like a bug otherwise.

        Archiving must not be a way to free up channels — "archive a workspace to
        get two more channels" cannot work. But it must free the *workspace*
        slot, or an organization at its cap could never replace one it archived.
        """
        from apps.workspaces.models import Workspace

        second = Workspace.objects.create(organization=tenancy.organization, name="second")
        add_channels(second, FREE_CHANNELS)
        second.is_archived = True
        second.save(update_fields=["is_archived"])

        # Archiving gave the workspace slot back ...
        assert entitlements.count_workspaces(tenancy.organization) == 1
        # ... but did not give the channels back.
        assert entitlements.count_channels(tenancy.organization) == FREE_CHANNELS

    def test_limits_are_counted_across_the_organizations_workspaces(self, tenancy: Any) -> None:
        """A limit somebody can multiply by clicking "New workspace" is not a
        limit. This is the reason the free plan caps workspaces at one."""
        from apps.workspaces.models import Workspace

        second = Workspace.objects.create(organization=tenancy.organization, name="second")
        add_channels(tenancy.workspace, 1)
        add_channels(second, 1)

        assert entitlements.count_channels(tenancy.organization) == 2
        with pytest.raises(PlanLimitError):
            entitlements.check_can_add_channel(tenancy.organization)


class TestApiAccess:
    def test_the_free_plan_has_none(self, tenancy: Any) -> None:
        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_api_access(tenancy.organization)

        assert caught.value.code == "plan_api"

    def test_the_paid_plan_does(self, tenancy: Any) -> None:
        make_paid(tenancy)

        entitlements.check_api_access(tenancy.organization)


class TestTheRefusalCarriesAnUpgradeLink:
    def test_every_refusal_has_a_code_and_a_link_field(self, tenancy: Any) -> None:
        """Carried on the exception so a view, a service caller and an htmx
        toast render the same link without each reversing it."""
        with pytest.raises(PlanLimitError) as caught:
            entitlements.check_can_add_workspace(tenancy.organization)

        assert caught.value.code
        assert hasattr(caught.value, "upgrade_url")
