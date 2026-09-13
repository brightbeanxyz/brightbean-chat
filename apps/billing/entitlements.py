"""What an organization is allowed to do, and the one line that keeps it optional.

**Read :func:`billing_enabled` first.** Everything in this module funnels through
it, and it is the promise that keeps the AGPL product whole: a deployment that
has not configured Stripe has exactly one tier, with every feature available to
every user, exactly as ``docs/SPEC.md`` §1.1 has always said. That is not a
special case sprinkled through the checks below — it is a single early return in
:func:`limits_for`, so there is one line to read to know a self-hoster is
unlimited, and one line to test.

**This module deliberately fails open, and it is the only one here that does.**
``apps.members.roles.permissions_for_role`` denies on a role it does not
recognise; ``apps.api.auth.permissions_for_scopes`` grants nothing for a scope it
does not know; both argue that guessing permissively is how a stale row becomes a
privilege escalation. That reasoning does not transfer, because there is no
privilege at stake: the resource on the other side of these checks is the
operator's own data, not another tenant's. Failing closed when configuration is
missing costs a self-hoster a working install; failing open costs a hosted
customer a free month. Those are not comparable, and the cheap one is the one
that cannot be made right by a support email.

**The gates live here as explicit functions, not in the permission system.**
Folding entitlements into ``WorkspaceMembership.effective_permissions`` would put
them on the protocol ``apps.api.auth.VirtualMembership`` implements, and the
failure runs the *opposite* way from the one that module's docstring warns about:
a plan-derived key that stopped being granted would **revoke** capability from
every API key already issued, at downgrade, with nothing on the keys page
changing to explain it. A permission answers "may this member do this"; an
entitlement answers "has this organization paid for this". They fail in opposite
directions and must not share a channel.

A decorator was the other candidate and does not fit either: four of the five
limits are counts evaluated against a *specific proposed action* — an
organization with two flows must reach the publish view and an organization with
four must not succeed at it — and three of the call sites have no view at all.
``apps/contacts/imports.py`` runs in a worker, ``send_outbound`` is a service
function, and ``accept_invitation`` is reached from an unauthenticated route.

Counting convention: every limit is counted **across the organization's
workspaces**, because a plan is bought by an organization and a limit somebody
can multiply by clicking "New workspace" is not a limit. That is affordable
precisely because the free plan also caps workspaces at one, so for the
organization that has never paid the loop below has a single iteration. The loop
is a loop rather than ``Model.objects.unscoped().filter(workspace__organization=…)``
on purpose: ``.unscoped()`` is the cross-tenant escape hatch, and the module that
decides what an organization may do is the last one that should contain an
example of it for somebody to copy.
"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.urls import NoReverseMatch, reverse

from apps.billing.plans import LIMITS_BY_PLAN, UNLIMITED, Limits, PlanKey


class PlanLimitError(Exception):
    """A plan limit refused an action. The message is shown to the operator.

    Carries ``upgrade_url`` so every surface that catches this renders the same
    link without reversing it itself — a view, a service caller and an htmx
    toast otherwise each grow their own copy of that ``reverse`` call, and the
    third one spells it differently.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.upgrade_url = upgrade_url()


def upgrade_url() -> str:
    """The billing page, or ``""`` when no route answers to that name yet.

    Reversal is guarded because the page itself is owned by another change: the
    route lands with the redesign that builds ``organizations:billing``. Until
    then the exception still carries a usable message and simply has no link,
    which is a better failure than a ``NoReverseMatch`` raised from inside a
    refusal that was itself the correct answer.
    """
    try:
        return reverse("organizations:billing")
    except NoReverseMatch:
        return ""


def billing_enabled() -> bool:
    """Whether this deployment has been deliberately configured to charge.

    Derived in ``config/settings/base.py`` from the secret key and both price
    ids, by truthiness rather than presence — see the comment there about
    one-click deploy targets setting empty configuration variables.
    """
    return bool(getattr(settings, "STRIPE_ENABLED", False))


def plan_key(organization: Any) -> str:
    """``"free"`` or ``"paid"``. Every organization is paid when billing is off."""
    return PlanKey.PAID if is_paid(organization) else PlanKey.FREE


def is_paid(organization: Any) -> bool:
    """Whether this organization is entitled to the paid plan.

    Answers ``True`` for everyone when Stripe is unconfigured — including an
    organization carrying a ``canceled`` row, which is the state a deployment
    would be in if it had billed once and then turned Stripe off. Reading the row
    in that case would strand its customers on a free plan they never chose.
    """
    if not billing_enabled():
        return True
    from apps.billing.models import BillingCustomer

    row = BillingCustomer.objects.filter(organization=organization).only("status").first()
    return row is not None and row.is_entitled


def limits_for(organization: Any) -> Limits:
    """What this organization may do.

    The early return below is the whole off-switch. Everything else in this
    module goes through it.
    """
    if not billing_enabled():
        return UNLIMITED
    return LIMITS_BY_PLAN[plan_key(organization)]


def entitlements_for_request(request: Any) -> Limits:
    """:func:`limits_for` for the current organization, memoised on the request.

    Worth memoising and cheap to: it is a settings read plus one small query.
    The *usage counts* below are deliberately **not** cached — they are live
    aggregates for the same reason ``media_library.quotas.used_bytes`` is one,
    and a cached count is a limit that can be walked past by refreshing.
    """
    cached = getattr(request, "_billing_limits", None)
    if cached is not None:
        return cached
    organization = getattr(request, "org", None)
    limits = UNLIMITED if organization is None else limits_for(organization)
    request._billing_limits = limits
    return limits


# --- Counting -----------------------------------------------------------------
# Every count sums across the organization's workspaces. See the module
# docstring on why this is a loop and not one join through `.unscoped()`.


def _workspaces(organization: Any) -> list[Any]:
    """Every workspace in the organization, archived ones included.

    Archived workspaces count towards resource limits, because archiving is not
    deleting and "archive a workspace to free up channels" must not work. They
    do **not** count towards the workspace limit itself — see
    :func:`count_workspaces` — and that asymmetry is deliberate rather than an
    oversight: counting them there would mean an organization at its cap could
    never replace a workspace it had archived.
    """
    from apps.workspaces.models import Workspace

    return list(Workspace.objects.for_org(organization.pk))


def _resolved(organization: Any, workspaces: list[Any] | None) -> list[Any]:
    """The caller's workspace list, or one fetched now.

    Every counter takes the list as an optional argument so ``usage_for`` can
    resolve it once and hand it round; on its own each would query the workspace
    table again for the same rows.
    """
    return _workspaces(organization) if workspaces is None else workspaces


def count_channels(organization: Any, *, workspaces: list[Any] | None = None) -> int:
    """Connections that are not disabled, across the organization."""
    from apps.channels.models import ChannelConnection, ConnectionStatus

    return sum(
        ChannelConnection.objects.for_workspace(workspace).exclude(status=ConnectionStatus.DISABLED).count()
        for workspace in _resolved(organization, workspaces)
    )


def count_active_automations(organization: Any, *, workspaces: list[Any] | None = None) -> int:
    """Published flows, active sequences and enabled inbox rules, added up.

    One number rather than three, because "four active automations" is one
    number on ManyChat's pricing page and splitting it into three budgets would
    be a harder rule to explain than the one being copied.

    **Broadcast mini-flows are excluded**, and the exclusion is load-bearing:
    ``apps.broadcasts.services`` creates a ``Flow`` and publishes it for every
    broadcast, so without this a free organization would silently spend half its
    automation budget by sending two broadcasts. Excluded on the relation, not
    on the folder name — a folder called "Broadcasts" is something a user can
    create.
    """
    from apps.campaigns.models import Sequence, SequenceStatus
    from apps.flows.models import Flow, FlowStatus
    from apps.inbox.models import InboxRule

    total = 0
    for workspace in _resolved(organization, workspaces):
        total += Flow.objects.for_workspace(workspace).filter(status=FlowStatus.ACTIVE, broadcasts__isnull=True).count()
        total += Sequence.objects.for_workspace(workspace).filter(status=SequenceStatus.ACTIVE).count()
        total += InboxRule.objects.for_workspace(workspace).filter(enabled=True).count()
    return total


def count_seats(organization: Any, *, excluding_invitation: Any = None) -> int:
    """Accepted members plus invitations that are still live.

    Pending invitations count. Otherwise an organization at its seat limit sends
    ten invitations, every one of them passes the check at the moment it is
    created, and the limit is decided by who clicks first.

    ``excluding_invitation`` leaves one out, for the caller that is turning that
    invitation into a membership: counting both would charge one person two
    seats. See ``apps.members.services._check_plan_allows_seat``.
    """
    from django.utils import timezone

    from apps.members.models import Invitation, OrgMembership

    members = OrgMembership.objects.filter(organization=organization).count()
    # Revoking an invitation sets expires_at to now rather than adding a status
    # column (apps/members/models.py), so "live" is one clause, not two.
    pending = Invitation.objects.filter(
        organization=organization,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    )
    if excluding_invitation is not None:
        pending = pending.exclude(pk=excluding_invitation)
    return members + pending.count()


def count_workspaces(organization: Any, *, workspaces: list[Any] | None = None) -> int:
    """Workspaces that are not archived. See :func:`_workspaces` on the asymmetry.

    Takes an already-resolved list when the caller has one — ``usage_for`` has,
    twice over, and would otherwise issue a third query for a number it can
    count in Python.
    """
    from apps.workspaces.models import Workspace

    if workspaces is not None:
        return sum(1 for workspace in workspaces if not workspace.is_archived)
    return Workspace.objects.for_org(organization.pk).filter(is_archived=False).count()


# --- Guards -------------------------------------------------------------------
# Shaped after apps/media_library/quotas.py: a check_* that raises with copy the
# operator reads, so a call site is one line rather than a branch.


def _refuse(message: str, code: str) -> None:
    raise PlanLimitError(message, code=code)


def check_can_add_channel(organization: Any) -> None:
    """Refuse a new connection when the organization is at its channel limit.

    Also the guard for *re-enabling* a disabled one, which is the same act: an
    organization over its cap could otherwise disable three connections and turn
    them back on one at a time.
    """
    limit = limits_for(organization).channels
    if limit is None or count_channels(organization) < limit:
        return
    _refuse(
        f"Your plan connects {limit} channels. Upgrade to connect more, or disable one you are not using.",
        "plan_channels",
    )


def check_can_activate_automation(organization: Any) -> None:
    """Refuse publishing or activating when the automation budget is spent."""
    limit = limits_for(organization).active_automations
    if limit is None or count_active_automations(organization) < limit:
        return
    _refuse(
        f"Your plan runs {limit} automations at once. Upgrade, or switch one off to free up a slot.",
        "plan_automations",
    )


def check_can_add_seat(organization: Any, *, excluding_invitation: Any = None) -> None:
    """Refuse a new invitation, and refuse accepting one issued before a downgrade."""
    limit = limits_for(organization).seats
    if limit is None or count_seats(organization, excluding_invitation=excluding_invitation) < limit:
        return
    _refuse(
        f"Your plan includes {limit} user{'' if limit == 1 else 's'}. Upgrade to add more.",
        "plan_seats",
    )


def check_can_add_workspace(organization: Any) -> None:
    """Refuse a new workspace, and refuse un-archiving one."""
    limit = limits_for(organization).workspaces
    if limit is None or count_workspaces(organization) < limit:
        return
    _refuse(
        f"Your plan includes {limit} workspace{'' if limit == 1 else 's'}. Upgrade to add more.",
        "plan_workspaces",
    )


def check_api_access(organization: Any) -> None:
    """Refuse issuing a new API key or webhook.

    Issuance only. **Keys already issued keep working after a downgrade,
    forever** — revoking a credential because a card expired is the hazard
    ``apps.api.auth``'s ``SCOPE_PERMISSIONS`` docstring describes, seen from the
    other side: an integration that worked yesterday starts answering 403 and
    nothing the operator can see says why.
    """
    if limits_for(organization).api_access:
        return
    _refuse("The public API is available on the paid plan.", "plan_api")


# --- The read model the billing page renders ----------------------------------


@dataclass(frozen=True)
class Usage:
    """Every counted number, with the cap beside it. ``None`` caps are unlimited."""

    plan: str
    active_contacts: int
    active_contacts_limit: int | None
    channels: int
    channels_limit: int | None
    automations: int
    automations_limit: int | None
    seats: int
    seats_limit: int | None
    workspaces: int
    workspaces_limit: int | None


def usage_for(organization: Any) -> Usage:
    """Everything the billing page shows. The contract between the two halves."""
    from apps.billing.metering import active_contact_count, current_period

    limits = limits_for(organization)
    # Resolved once and passed down: the three counters below would otherwise
    # each query the workspace table for the same rows.
    workspaces = _workspaces(organization)
    return Usage(
        plan=plan_key(organization),
        active_contacts=active_contact_count(organization, period=current_period(organization)),
        active_contacts_limit=limits.active_contacts_per_month,
        channels=count_channels(organization, workspaces=workspaces),
        channels_limit=limits.channels,
        automations=count_active_automations(organization, workspaces=workspaces),
        automations_limit=limits.active_automations,
        seats=count_seats(organization),
        seats_limit=limits.seats,
        workspaces=count_workspaces(organization, workspaces=workspaces),
        workspaces_limit=limits.workspaces,
    )
