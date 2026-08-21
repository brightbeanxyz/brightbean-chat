"""notify() — fan-out, the email decision, and the transaction contract."""

import contextlib
import json
import smtplib
from typing import Any
from unittest.mock import patch

import pytest
from django.core import mail
from django.db import transaction
from django.test import override_settings

from apps.members.roles import WorkspaceRole
from apps.notifications.engine import notify
from apps.notifications.events import NotificationCopyError
from apps.notifications.mail import send_delivery
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


@pytest.mark.django_db
class TestEmptyRolesMeansNobody:
    """The mirror of test_an_empty_user_list_notifies_nobody. `roles or
    DEFAULT_ROLES` used truthiness, so a caller's filtered-to-empty role list
    silently became "every admin in the workspace"."""

    def test_an_empty_roles_tuple_notifies_nobody(self, tenancy):
        assert notify(tenancy.workspace, "flow_loop_cap_hit", roles=(), context=LOOP_CAP_CONTEXT) == []
        assert Notification.objects.count() == 0

    def test_an_empty_roles_list_notifies_nobody(self, tenancy):
        assert notify(tenancy.workspace, "flow_loop_cap_hit", roles=[], context=LOOP_CAP_CONTEXT) == []

    def test_roles_none_still_defaults_to_admins(self, tenancy):
        created = notify(tenancy.workspace, "flow_loop_cap_hit", roles=None, context=LOOP_CAP_CONTEXT)

        assert {n.user for n in created} == {tenancy.owner, tenancy.members["admin"]}


@pytest.mark.django_db
class TestPayloadsAreNotShared:
    def test_each_row_gets_its_own_dict(self, tenancy):
        """All three rows aliased one dict, so mutating one caller-side
        appeared to change rows that were never written."""
        created = loop_cap(tenancy.workspace)
        assert len(created) == 2

        created[0].payload["only_mine"] = True

        assert "only_mine" not in created[1].payload

    def test_nested_values_are_not_shared_either(self, tenancy):
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            context={"flow_name": "W", "tags": ["a"]},
        )

        created[0].payload["tags"].append("b")

        assert created[1].payload["tags"] == ["a"]


@pytest.mark.django_db
class TestContextValuesMustSurviveJsonb:
    def test_a_nested_uuid_is_dropped_rather_than_crashing_the_write(self, tenancy):
        """A list is a JSON type, so a shallow check let a list of UUIDs
        through and the TypeError landed at create() — part-way through a
        fan-out that had already written rows."""
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            context={"flow_name": "W", "contacts": [tenancy.workspace.pk]},
        )

        assert len(created) == 2
        assert "contacts" not in created[0].payload
        assert created[0].payload["flow_name"] == "W"

    def test_a_nested_dict_of_scalars_survives(self, tenancy):
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            context={"flow_name": "W", "stats": {"sent": 1, "failed": 0, "note": None}},
        )

        assert created[0].payload["stats"] == {"sent": 1, "failed": 0, "note": None}

    def test_a_non_string_key_is_dropped(self, tenancy):
        """json.dumps would coerce it silently, so the value would round-trip
        as something the caller did not write."""
        created = notify(tenancy.workspace, "flow_loop_cap_hit", context={"flow_name": "W", "counts": {1: "one"}})

        assert "counts" not in created[0].payload

    def test_a_self_referential_structure_is_refused_not_recursed(self, tenancy):
        loop: dict[str, Any] = {"flow_name": "W"}
        loop["self"] = loop

        created = notify(tenancy.workspace, "flow_loop_cap_hit", context=loop)

        assert "self" not in created[0].payload

    def test_every_stored_payload_actually_serialises(self, tenancy):
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            context={"flow_name": "W", "ok": [1, "two", {"three": True}]},
        )

        json.dumps(created[0].payload)


@pytest.mark.django_db
class TestWorkspaceMayBeAnId:
    """_payload and _dispatch_emails both tolerated a bare id; the roles path
    dereferenced .organization_id and crashed. A queue worker rehydrating a job
    holds an id, not an instance."""

    def test_a_workspace_id_resolves_on_the_roles_path(self, tenancy):
        created = notify(tenancy.workspace.pk, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert {n.user for n in created} == {tenancy.owner, tenancy.members["admin"]}

    def test_a_workspace_id_still_fills_the_payload_name(self, tenancy):
        """Which getattr(workspace, "name", "") left blank before."""
        created = notify(tenancy.workspace.pk, "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)

        assert created[0].payload["workspace_name"] == tenancy.workspace.name
        assert created[0].payload["workspace_id"] == str(tenancy.workspace.pk)

    def test_an_id_naming_nothing_raises_clearly(self, tenancy):
        import uuid

        with pytest.raises(ValueError, match="neither a Workspace nor the id of one"):
            notify(uuid.uuid4(), "flow_loop_cap_hit", context=LOOP_CAP_CONTEXT)


@pytest.mark.django_db
class TestSubjectCannotBreakTheHeader:
    def test_a_newline_in_the_title_does_not_escape_as_a_bad_header(self, tenancy, django_capture_on_commit_callbacks):
        """BadHeaderError is a ValueError, so it slipped past the
        transport-only except clause and escaped between the in-memory attempts
        increment and the save — leaving the row PENDING with attempts=0."""
        with django_capture_on_commit_callbacks(execute=True):
            notify(
                tenancy.workspace,
                "flow_loop_cap_hit",
                users=[tenancy.owner],
                context={"flow_name": "Welcome\nseries\r\nBcc: attacker@evil.test"},
            )

        message = mail.outbox[0].message()
        assert len(mail.outbox) == 1
        # The subject is one line, so the injected text is inert content rather
        # than a header break.
        assert "\n" not in mail.outbox[0].subject and "\r" not in mail.outbox[0].subject
        assert message["Bcc"] is None
        assert message["To"] == tenancy.owner.email
        assert NotificationDelivery.objects.get().status == DeliveryStatus.SENT

    def test_a_non_dict_payload_does_not_crash_the_send(self, tenancy, django_capture_on_commit_callbacks):
        """payload is a JSONField; a row edited elsewhere can hold a list."""
        created = notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context={"flow_name": "W"})
        Notification.objects.filter(pk=created[0].pk).update(payload=["not", "a", "dict"])
        delivery = NotificationDelivery.objects.get()
        delivery.notification.refresh_from_db()

        assert send_delivery(delivery) is True
        # Falls back to the title, and the action URL falls back to a route we
        # control, rather than raising AttributeError out of a narrow except.
        assert mail.outbox[-1].subject == 'Flow "W" hit the loop cap'
