"""``accept_invitation`` is atomic, and stays that way.

This exists because it briefly was not. A helper inserted above it landed
*between* ``@transaction.atomic`` and the ``def``, so the decorator wrapped the
helper and the function silently lost the property its own docstring claims —
"Atomic, unlike Studio's" — along with the row lock that enforces single use.
Nothing failed: the tests all passed, because nothing asserted the transaction.

Both halves are checked. The structural one catches the decorator moving again;
the behavioural one catches somebody keeping the decorator and removing the
atomicity some other way.
"""

import inspect
from typing import Any

import pytest
from django.db import transaction

from apps.members import services

pytestmark = pytest.mark.django_db


class TestItRunsInATransaction:
    def test_the_decorator_is_on_accept_invitation_itself(self) -> None:
        """The exact failure that happened: a decorator separated from its
        function by an insertion, which reads as fine in a diff."""
        source = inspect.getsource(services)
        index = source.index("def accept_invitation(")
        preceding = source[:index].rstrip().splitlines()[-1].strip()

        assert preceding == "@transaction.atomic", (
            f"accept_invitation is preceded by {preceding!r}, not its @transaction.atomic decorator"
        )

    def test_the_body_really_runs_inside_an_atomic_block(self, tenancy: Any, user: Any, monkeypatch: Any) -> None:
        """Structure is not behaviour.

        ``_check_plan_allows_seat`` is called from inside ``accept_invitation``'s
        body, so recording the connection state from there observes the real
        call rather than a decorator's presence. Before the fix this recorded
        False.
        """
        from datetime import timedelta

        from django.utils import timezone

        from apps.members.models import Invitation
        from apps.members.roles import OrgRole

        observed: list[bool] = []
        monkeypatch.setattr(
            services,
            "_check_plan_allows_seat",
            lambda *a, **k: observed.append(transaction.get_connection().in_atomic_block),
        )

        invitation = Invitation(
            organization=tenancy.organization,
            email=user.email,
            org_role=OrgRole.MEMBER,
            invited_by=tenancy.owner,
            expires_at=timezone.now() + timedelta(days=7),
        )
        invitation.issue_token()
        invitation.save()

        services.accept_invitation(invitation, user)

        assert observed == [True], "accept_invitation ran its body outside a transaction"
