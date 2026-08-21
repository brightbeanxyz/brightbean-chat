"""Regressions for the review findings in apps/accounts."""

import inspect
import smtplib

import pytest
from django.test import RequestFactory

from apps.accounts import services
from apps.accounts.adapters import AccountAdapter
from apps.members.models import OrgMembership
from apps.organizations.models import Organization
from tests.support import create_user


@pytest.mark.django_db
class TestProvisioningIsSerialised:
    """ensure_provisioned runs from a GET handler now, so two concurrent first
    visits could both pass an unlocked exists() guard and each create an
    organization — leaving the account in two, with nothing to say which is
    real. Studio ran it inside the user-creation transaction, which is what kept
    the window shut there."""

    def test_it_takes_a_row_lock_before_reading_the_guard(self):
        source = inspect.getsource(services.provision_organization_and_workspace)
        lock_at = source.index("select_for_update")
        guard_at = source.index("OrgMembership.objects.filter(user=user)")

        assert lock_at < guard_at, "the guard must be read under the lock, not before it"

    def test_it_runs_in_a_transaction(self):
        assert getattr(services.provision_organization_and_workspace, "_non_atomic_requests", None) is None
        assert "atomic" in inspect.getsource(services).split("def provision_organization_and_workspace")[0][-400:]

    def test_repeated_calls_create_exactly_one_organization(self):
        user = create_user("solo@example.test")

        for _ in range(3):
            services.provision_organization_and_workspace(user)

        assert OrgMembership.objects.filter(user=user).count() == 1
        assert Organization.objects.count() == 1

    def test_a_user_deleted_mid_flight_provisions_nothing(self):
        user = create_user("ghost@example.test")
        pk = user.pk
        type(user).objects.filter(pk=pk).delete()

        assert services.provision_organization_and_workspace(user) is None
        assert Organization.objects.count() == 0


class TestMailFailuresAreNarrow:
    """A bare `except Exception` turned a broken email template into an
    invisible non-delivery, reported as an SMTP problem."""

    def _adapter(self):
        return AccountAdapter()

    def test_delivery_failures_are_swallowed(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise smtplib.SMTPServerDisconnected("gone")

        monkeypatch.setattr("allauth.account.adapter.DefaultAccountAdapter.send_mail", refuse)

        self._adapter().send_mail("account/email/x", "a@b.test", {})

    def test_a_broken_template_still_raises(self, monkeypatch):
        from django.template import TemplateSyntaxError

        def explode(*args, **kwargs):
            raise TemplateSyntaxError("Invalid block tag")

        monkeypatch.setattr("allauth.account.adapter.DefaultAccountAdapter.send_mail", explode)

        with pytest.raises(TemplateSyntaxError):
            self._adapter().send_mail("account/email/x", "a@b.test", {})

    def test_the_invitation_mailer_is_narrow_too(self):
        from apps.members import services as member_services

        source = inspect.getsource(member_services.send_invite_email)

        assert "except (OSError, smtplib.SMTPException)" in source
        assert "except Exception" not in source


@pytest.mark.django_db
class TestSwitcherWritesItsOwnState:
    """The view used to be empty, relying entirely on RBACMiddleware's
    keep-in-sync side effect."""

    def test_the_view_performs_the_write_without_the_middleware(self, tenancy):
        from apps.workspaces.views import switch

        request = RequestFactory().post(f"/w/{tenancy.workspace.pk}/switch/")
        request.user = tenancy.owner
        request.workspace = tenancy.workspace
        tenancy.owner.last_workspace_id = None
        tenancy.owner.save(update_fields=["last_workspace_id"])

        # The undecorated view, so no middleware and no decorators run.
        switch.__wrapped__.__wrapped__(request, str(tenancy.workspace.pk))

        tenancy.owner.refresh_from_db()
        assert tenancy.owner.last_workspace_id == tenancy.workspace.pk
