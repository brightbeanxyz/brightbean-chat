"""The set-wise variant agrees with the per-identity one, row by row.

SPEC §13.2 applies these rules "set-wise before fanout and records
skipped_window counts", and two implementations of a compliance rule is two
chances to disagree. The failure mode is not abstract: a broadcast whose preview
count includes people the send then refuses, or — the direction that matters —
one whose preview excludes people the send happily messages anyway.

``compliance._rules()`` is shared, so ordering and the short-circuits are the
same objects in both paths. What is *not* structurally shared is each rule's two
spellings — a ``Q`` and a callable — and this module is what closes that.
"""

from datetime import timedelta
from itertools import product
from typing import Any

import pytest
from django.utils import timezone

from apps.channels import policy as channel_policy
from apps.channels.events import OutboundMessage, TextBlock
from apps.contacts.services import create_contact
from apps.messaging.compliance import (
    DECISION_FIELD,
    Allowed,
    annotate_eligibility,
    can_send,
    eligible,
)
from apps.messaging.models import ContactChannelIdentity, MessageSource
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db

TEXT = OutboundMessage(blocks=(TextBlock(text="hi"),))
TAGGED = OutboundMessage(tag="ACCOUNT_UPDATE")
TEMPLATED = OutboundMessage(template_ref="tpl-1")
OUTBOUNDS = {"plain": TEXT, "tagged": TAGGED, "templated": TEMPLATED}

#: Every interesting identity state, named so a failure says which one.
STATES: dict[str, dict[str, Any]] = {
    "fresh": {},
    "window_closed": {"window_hours": -1},
    "never_inbound": {"window_hours": None, "last_inbound_days": None},
    "recent_inbound_closed_window": {"window_hours": -1, "last_inbound_days": 3},
    "old_inbound_closed_window": {"window_hours": -1, "last_inbound_days": 30},
    "opted_out": {"opted_out": True},
    "no_consent": {"opt_in": False},
}

#: A pending identity has no connection to send through (contract 1). It lives
#: outside STATES because make_identity attaches one to every row it builds.
PENDING = "pending"


def make_identity(
    contact: Any,
    connection: Any,
    *,
    window_hours: float | None = 1,
    last_inbound_days: float | None = 0,
    opt_in: bool = True,
    opted_out: bool = False,
) -> ContactChannelIdentity:
    now = timezone.now()
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=connection.platform,
        platform_user_id=f"u-{contact.pk}",
        opt_in=opt_in,
        opt_in_at=now if opt_in else None,
        opt_in_source="message_in" if opt_in else "",
        opted_out_at=now if opted_out else None,
        window_expires_at=None if window_hours is None else now + timedelta(hours=window_hours),
        last_inbound_at=None if last_inbound_days is None else now - timedelta(days=last_inbound_days),
    )


def code_of(decision: Any) -> str:
    return decision.code


@pytest.mark.parametrize(
    ("platform", "source", "shape"),
    list(product(sorted(channel_policy.POLICIES), [s.value for s in MessageSource], sorted(OUTBOUNDS))),
)
def test_the_two_evaluators_agree_for_every_state(tenancy: Any, platform: str, source: str, shape: str) -> None:
    connection = make_connection(tenancy.workspace, platform=platform, suffix=f"{platform}-{source}-{shape}")
    outbound = OUTBOUNDS[shape]
    now = timezone.now()

    expected: dict[Any, str] = {}
    for name, state in STATES.items():
        contact = create_contact(tenancy.workspace, first_name=name)
        ident = make_identity(contact, connection, **state)
        expected[ident.pk] = code_of(can_send(ident, source, outbound, now=now))

    # The connection-less case. annotate_eligibility used to filter these out
    # entirely, which made the NO_CONNECTION rule unreachable set-wise while it
    # was reachable per-identity — and left a broadcast preview quietly omitting
    # those people instead of counting them under a skip reason.
    pending_contact = create_contact(tenancy.workspace, first_name=PENDING)
    pending = ContactChannelIdentity.objects.create(
        contact=pending_contact,
        channel_connection=None,
        platform=connection.platform,
        platform_user_id=f"u-{pending_contact.pk}",
        opt_in=True,
        opt_in_at=now,
        opt_in_source="data_collection",
    )
    expected[pending.pk] = code_of(can_send(pending, source, outbound, now=now))

    annotated = annotate_eligibility(
        ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
        connection=connection,
        source=source,
        outbound=outbound,
        now=now,
    )
    actual = {row.pk: getattr(row, DECISION_FIELD) for row in annotated}

    assert actual == expected


class TestTheQuerysetContract:
    def test_it_never_scopes_for_the_caller(self, tenancy: Any) -> None:
        """The guard has to stay the caller's, or a forgotten for_workspace()
        would be silently forgiven on the one path that fans out to thousands of
        people."""
        from apps.common.scoping import UnscopedQueryError

        connection = make_connection(tenancy.workspace, suffix="unscoped")
        with pytest.raises(UnscopedQueryError):
            list(
                annotate_eligibility(
                    ContactChannelIdentity.objects.all(),
                    connection=connection,
                    source="broadcast",
                    outbound=TEXT,
                )
            )

    def test_eligible_returns_only_the_allowed(self, tenancy: Any) -> None:
        connection = make_connection(tenancy.workspace, suffix="eligible")
        allowed = make_identity(create_contact(tenancy.workspace, first_name="ok"), connection)
        make_identity(create_contact(tenancy.workspace, first_name="gone"), connection, opted_out=True)

        rows = eligible(
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
            connection=connection,
            source="broadcast",
            outbound=TEXT,
        )
        assert [row.pk for row in rows] == [allowed.pk]

    def test_it_counts_skips_by_reason(self, tenancy: Any) -> None:
        """SPEC §13.2's skipped counts fall straight out of the annotation."""
        from django.db.models import Count

        connection = make_connection(tenancy.workspace, suffix="counts")
        make_identity(create_contact(tenancy.workspace, first_name="ok"), connection)
        make_identity(create_contact(tenancy.workspace, first_name="a"), connection, opted_out=True)
        make_identity(create_contact(tenancy.workspace, first_name="b"), connection, opted_out=True)

        counts = dict(
            annotate_eligibility(
                ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
                connection=connection,
                source="broadcast",
                outbound=TEXT,
            )
            .values_list(DECISION_FIELD)
            .annotate(total=Count("id"))
        )
        assert counts["opted_out"] == 2

    def test_a_pending_identity_is_counted_as_a_skip_not_dropped(self, tenancy: Any) -> None:
        """An operator sizing a broadcast needs to see "we hold this address but
        have no connection for it", not a silently smaller number."""
        from apps.messaging.codes import Denial

        connection = make_connection(tenancy.workspace, suffix="pending")
        contact = create_contact(tenancy.workspace, first_name="captured early")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=None,
            platform=connection.platform,
            platform_user_id="+15550101234",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="data_collection",
        )
        row = annotate_eligibility(
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
            connection=connection,
            source="broadcast",
            outbound=TEXT,
        ).get()
        assert getattr(row, DECISION_FIELD) == Denial.NO_CONNECTION

    def test_a_pending_identity_on_another_platform_is_out_of_scope(self, tenancy: Any) -> None:
        """The widening is per platform: an SMS address says nothing about who a
        Telegram broadcast can reach."""
        connection = make_connection(tenancy.workspace, suffix="scope-platform")
        contact = create_contact(tenancy.workspace, first_name="elsewhere")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=None,
            platform="sms",
            platform_user_id="+15550101234",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="data_collection",
        )
        rows = annotate_eligibility(
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
            connection=connection,
            source="broadcast",
            outbound=TEXT,
        )
        assert rows.count() == 0

    def test_another_connections_identities_are_excluded(self, tenancy: Any) -> None:
        """The narrowing is what makes deriving one PlatformPolicy sound."""
        first = make_connection(tenancy.workspace, suffix="one")
        second = make_connection(tenancy.workspace, suffix="two")
        make_identity(create_contact(tenancy.workspace, first_name="here"), first)
        make_identity(create_contact(tenancy.workspace, first_name="elsewhere"), second)

        rows = annotate_eligibility(
            ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
            connection=first,
            source="automation",
            outbound=TEXT,
        )
        assert rows.count() == 1

    def test_it_compiles_through_the_orm_with_no_string_sql(self, tenancy: Any) -> None:
        """SECURITY-BASELINE §7: set-wise evaluation is ORM-only."""
        connection = make_connection(tenancy.workspace, suffix="sql")
        compiled = str(
            annotate_eligibility(
                ContactChannelIdentity.objects.for_workspace(tenancy.workspace),
                connection=connection,
                source="automation",
                outbound=TEXT,
            ).query
        )
        assert "CASE WHEN" in compiled
        assert "workspace_id" in compiled


class TestConsistencyUnderChange:
    def test_a_platform_added_to_the_policy_table_is_covered_automatically(self) -> None:
        """The parametrisation is built from POLICIES itself, so a Layer-5 row
        enters this matrix without anybody editing this file."""
        from apps.common.platforms import Platform

        assert set(channel_policy.POLICIES) == set(Platform.values)

    def test_every_grant_code_is_an_allowed_decision(self) -> None:
        from apps.messaging.codes import Grant
        from apps.messaging.compliance import ALLOWED_CODES

        assert {grant.value for grant in Grant} == set(ALLOWED_CODES)

    def test_an_allowed_decision_is_the_only_thing_that_sends(self) -> None:
        """The send pipeline branches on isinstance(decision, Allowed), so a
        decision type that is neither Allowed nor a refusal cannot exist."""
        from apps.messaging.compliance import Blocked, NeedsTag, NeedsTemplate

        for kind in (Blocked, NeedsTemplate, NeedsTag):
            assert not issubclass(kind, Allowed)
