"""notify() — fan-out, the email decision, and the transaction contract."""

import contextlib
import smtplib
from unittest.mock import patch

import pytest
from django.core import mail
from django.db import transaction
from django.test import override_settings

from apps.members.roles import WorkspaceRole
from apps.notifications.engine import notify
from apps.notifications.events import NotificationCopyError
from apps.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationSetting,
)

LOOP_CAP_CONTEXT = {"flow_name": "Welcome", "contact_name": "Ada"}


def loop_cap(workspace, **kwargs):
    return notify(workspace, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT, **kwargs)


@pytest.mark.django_db
class TestFanOut:
    def test_the_default_audience_is_workspace_admins(self, tenancy):
        """SPEC §9.2's "notify workspace admins" is the signature's default, so
        the loop-cap caller in L3-B does not have to spell it."""
        created = loop_cap(tenancy.workspace)

        assert {n.user for n in created} == {tenancy.owner, tenancy.members["admin"]}

    def test_one_row_per_recipient_and_the_rows_are_returned(self, tenancy):
        created = loop_cap(tenancy.workspace)

        assert len(created) == Notification.objects.count() == 2
        assert all(isinstance(n, Notification) for n in created)

    def test_the_copy_comes_from_the_registry_not_the_caller(self, tenancy):
        notification = loop_cap(tenancy.workspace)[0]

        assert notification.title == 'Flow "Welcome" hit the loop cap'
        assert "Ada" in notification.body

    def test_explicit_users_override_role_resolution(self, tenancy):
        created = loop_cap(tenancy.workspace, users=[tenancy.members["viewer"]])

        assert [n.user for n in created] == [tenancy.members["viewer"]]

    def test_an_empty_user_list_notifies_nobody(self, tenancy):
        """`users=[]` must not fall through to "all admins". The guard is
        `users is not None`, and this is the test that keeps it that way."""
        assert loop_cap(tenancy.workspace, users=[]) == []
        assert Notification.objects.count() == 0

    def test_users_and_roles_together_is_a_type_error(self, tenancy):
        """A call-signature error, so it raises in production too — unlike an
        unknown event type, there is no webhook to protect from it."""
        with pytest.raises(TypeError, match="not both"):
            loop_cap(tenancy.workspace, users=[tenancy.owner], roles=(WorkspaceRole.ADMIN,))

    def test_a_workspace_with_nobody_to_tell_is_not_an_error(self, tenancy):
        created = loop_cap(tenancy.workspace, roles=(WorkspaceRole.AGENT,), users=None)

        assert [n.user for n in created] == [tenancy.members["agent"]]


@pytest.mark.django_db
class TestPayload:
    def test_the_workspace_is_denormalised_into_the_payload(self, tenancy):
        notification = loop_cap(tenancy.workspace)[0]

        assert notification.payload["workspace_id"] == str(tenancy.workspace.pk)
        assert notification.payload["workspace_name"] == tenancy.workspace.name

    def test_the_context_round_trips(self, tenancy):
        notification = loop_cap(tenancy.workspace)[0]

        assert notification.payload["flow_name"] == "Welcome"

    def test_the_registry_supplies_the_icon_and_tone(self, tenancy):
        notification = loop_cap(tenancy.workspace)[0]

        assert notification.payload["icon"] == "flows"
        assert notification.payload["tone"] == "error"

    def test_unserialisable_context_values_are_dropped_not_crashed(self, tenancy):
        """payload is jsonb. A UUID or a model instance in the context is a
        caller slip, and losing that key beats losing the notification."""
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            context={"flow_name": "Welcome", "flow": tenancy.workspace},
        )

        assert "flow" not in created[0].payload
        assert created[0].payload["flow_name"] == "Welcome"


@pytest.mark.django_db
class TestUnknownEventType:
    def test_production_drops_it_and_writes_nothing(self, tenancy, caplog):
        with caplog.at_level("ERROR", logger="apps.notifications.events"):
            assert notify(tenancy.workspace, "not_a_real_event", context={}) == []

        assert Notification.objects.count() == 0
        assert "not_a_real_event" in caplog.text

    @override_settings(DEBUG=True)
    def test_debug_raises(self, tenancy):
        with pytest.raises(NotificationCopyError):
            notify(tenancy.workspace, "not_a_real_event", context={})


@pytest.mark.django_db
class TestEmail:
    def test_an_email_event_renders_both_parts_and_sends(self, tenancy, django_capture_on_commit_callbacks):
        """The synchronous path, which is what runs until issue #5 merges.

        The send is wrapped in transaction.on_commit, and pytest.mark.django_db
        never commits — so without capturing the callbacks the outbox stays
        empty and this test would silently prove nothing.
        """
        with django_capture_on_commit_callbacks(execute=True):
            loop_cap(tenancy.workspace)

        assert len(mail.outbox) == 2
        message = mail.outbox[0]
        assert message.subject == 'Flow "Welcome" hit the loop cap'
        assert "Welcome" in message.body
        alternative, mime = message.alternatives[0]
        assert mime == "text/html"
        assert "<!DOCTYPE html>" in alternative

    def test_the_email_links_back_with_an_absolute_url(self, tenancy, django_capture_on_commit_callbacks):
        with (
            override_settings(APP_URL="https://chat.example.test/"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            loop_cap(tenancy.workspace)

        assert "https://chat.example.test/notifications/" in mail.outbox[0].body

    def test_a_delivery_row_records_the_outcome(self, tenancy, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            loop_cap(tenancy.workspace)

        delivery = NotificationDelivery.objects.first()
        assert delivery.channel == Channel.EMAIL
        assert delivery.status == DeliveryStatus.SENT
        assert delivery.sent_at is not None
        assert delivery.attempts == 1

    def test_no_delivery_row_is_written_for_the_in_app_channel(self, tenancy):
        """The Notification row IS the in-app delivery. Studio writes a second
        row and dispatches it to a function whose body is `pass`."""
        loop_cap(tenancy.workspace)

        assert not NotificationDelivery.objects.filter(channel=Channel.IN_APP).exists()

    def test_opting_out_suppresses_the_email_but_keeps_the_in_app_row(
        self, tenancy, django_capture_on_commit_callbacks
    ):
        NotificationSetting.objects.create(user=tenancy.owner, email_enabled=False)

        with django_capture_on_commit_callbacks(execute=True):
            loop_cap(tenancy.workspace)

        assert Notification.objects.filter(user=tenancy.owner).exists()
        assert [m.to for m in mail.outbox] == [[tenancy.members["admin"].email]]

    def test_an_event_the_registry_marks_in_app_only_never_mails(self, tenancy, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            notify(tenancy.workspace, "broadcast_finished", context={"broadcast_name": "Launch"})

        assert Notification.objects.exists()
        assert mail.outbox == []

    def test_the_opt_out_check_is_one_query_for_the_whole_fan_out(self, tenancy, django_assert_max_num_queries):
        """Studio asks per recipient, which is O(N) queries to answer a question
        that already has N ids in it."""
        with django_assert_max_num_queries(12):
            loop_cap(tenancy.workspace)

    def test_an_smtp_failure_is_recorded_and_does_not_lose_the_notification(
        self, tenancy, django_capture_on_commit_callbacks, caplog
    ):
        with (
            patch(
                "django.core.mail.EmailMultiAlternatives.send",
                side_effect=smtplib.SMTPException("mail server on fire"),
            ),
            caplog.at_level("ERROR", logger="apps.notifications.mail"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            created = loop_cap(tenancy.workspace)

        assert len(created) == 2
        assert Notification.objects.count() == 2
        assert set(NotificationDelivery.objects.values_list("status", flat=True)) == {DeliveryStatus.FAILED}
        assert "mail server on fire" in NotificationDelivery.objects.first().error_message

    def test_the_recipient_address_is_not_written_to_the_log(self, tenancy, django_capture_on_commit_callbacks, caplog):
        """The address is personal data; apps/accounts/adapters.py omits it for
        the same reason."""
        with (
            patch(
                "django.core.mail.EmailMultiAlternatives.send",
                side_effect=smtplib.SMTPException("nope"),
            ),
            caplog.at_level("ERROR", logger="apps.notifications.mail"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            loop_cap(tenancy.workspace)

        assert tenancy.owner.email not in caplog.text

    def test_a_template_error_is_not_swallowed_as_a_delivery_failure(self, tenancy, django_capture_on_commit_callbacks):
        """The narrow `except (OSError, SMTPException)` is the point: a renamed
        context key reported as an SMTP problem would leave every notification
        email in the deployment silently missing."""
        with (
            patch(
                "apps.notifications.mail.render_to_string",
                side_effect=TypeError("template blew up"),
            ),
            pytest.raises(TypeError, match="template blew up"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            loop_cap(tenancy.workspace)


@pytest.mark.django_db(transaction=True)
class TestTransactionContract:
    def test_a_rolled_back_caller_has_not_already_mailed_anyone(self, tenancy):
        """notify() opens no transaction of its own — the flow engine (SPEC
        §9.6) runs inside one, and a notification must not roll back a flow
        step. The send is deferred to on_commit so the reverse cannot happen
        either: an email that went out for work that did not."""
        with contextlib.suppress(RuntimeError), transaction.atomic():
            loop_cap(tenancy.workspace)
            raise RuntimeError("caller changed its mind")

        assert Notification.objects.count() == 0
        assert mail.outbox == []
