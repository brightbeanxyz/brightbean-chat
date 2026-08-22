"""The table's shape — the parts other code depends on and migrations can break."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.scoping import UnscopedQueryError
from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import make_action
from tests.support import Tenancy


@pytest.mark.django_db
class TestScheduledActionTable:
    def test_db_table_matches_the_hand_written_claim_sql(self) -> None:
        """The claim statement names the table as a literal, so pin the pair.

        ``apps.queueing.worker.CLAIM_SQL`` cannot interpolate ``_meta.db_table``
        without becoming a string-built query. This test is the other half of
        that trade: rename the table and this fails immediately rather than the
        worker failing at runtime in production.
        """
        from apps.queueing.worker import CLAIM_SQL

        assert ScheduledAction._meta.db_table == "queueing_scheduled_action"
        assert ScheduledAction._meta.db_table in CLAIM_SQL

    def test_idempotency_key_is_unique(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace, idempotency_key="dupe")
        with pytest.raises(IntegrityError), transaction.atomic():
            make_action(tenancy.workspace, idempotency_key="dupe")

    def test_many_rows_may_omit_the_idempotency_key(self, tenancy: Tenancy) -> None:
        """NULLs are distinct in a Postgres unique index; "" would not be."""
        make_action(tenancy.workspace)
        make_action(tenancy.workspace)
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 2


@pytest.mark.django_db
class TestTenantScoping:
    def test_an_unscoped_query_still_raises(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace)
        with pytest.raises(UnscopedQueryError):
            list(ScheduledAction.objects.filter(status=ActionStatus.PENDING))

    def test_one_workspace_cannot_see_anothers_actions(self, tenancy: Tenancy, other_tenancy: Tenancy) -> None:
        make_action(tenancy.workspace)
        make_action(other_tenancy.workspace)
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 1

    def test_system_rows_are_invisible_to_every_tenant_query(self, tenancy: Tenancy) -> None:
        """The security property that makes a nullable workspace the safe choice.

        A deployment-level job has no workspace, and ``for_workspace()`` filters
        on a concrete id — so no tenant query matches it, whoever writes one.
        Only ``.unscoped()`` sees it, which is greppable by design.
        """
        system = make_action(None, type="housekeeping")

        assert ScheduledAction.objects.for_workspace(tenancy.workspace).count() == 0
        assert ScheduledAction.objects.unscoped().filter(pk=system.pk).exists()
        assert system.is_system

    def test_deleting_a_workspace_takes_its_queued_work_with_it(self, tenancy: Tenancy) -> None:
        make_action(tenancy.workspace)
        system = make_action(None, type="housekeeping")

        tenancy.workspace.delete()

        remaining = list(ScheduledAction.objects.unscoped())
        assert [row.pk for row in remaining] == [system.pk]


@pytest.mark.django_db
def test_ordering_is_by_run_at(tenancy: Tenancy) -> None:
    now = timezone.now()
    later = make_action(tenancy.workspace, run_at=now + timedelta(minutes=5))
    sooner = make_action(tenancy.workspace, run_at=now)
    assert list(ScheduledAction.objects.for_workspace(tenancy.workspace)) == [sooner, later]
