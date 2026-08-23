"""SPEC §11.9's ``send_sms`` node — the crossing, and what it refuses to invent.

Exercised through the runner rather than by calling ``execute``, like the rest of
the node suite: a node's output is a ``StepResult``, but its *meaning* is the
edge the runner follows, and a node that returned the right result against a
handle nobody drew would pass a unit test and route nobody.

The two things worth being careful about are both refusals. This node sends on
the workspace's **SMS** connection rather than the one the run is happening on,
so it has to find one and find the contact's number on it — and when either is
missing it follows ``error`` rather than guessing. In particular it never turns
``contact.phone`` into an identity: a number typed into a CRM field is not
consent to text it (SPEC §11.8), and fabricating one would route straight past
the compliance engine's ``no_opt_in`` rule.
"""

from typing import Any

import pytest

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.flows.engine import start_flow
from apps.flows.engine.registry import synchronous_safe
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import (
    FakeFacade,
    FakeMessage,
    connection_for,
    contact_for,
    edge,
    graph,
    node,
    published_flow,
)

pytestmark = pytest.mark.django_db

NUMBER = "+15557778888"


def sms_flow(workspace: Any, config: dict[str, Any] | None = None) -> Any:
    """A ``send_sms`` whose two handles lead to distinguishable places."""
    return published_flow(
        workspace,
        graph(
            [
                node("sms", "send_sms", config or {"text": "Your code is 1234."}),
                node("ok", "action", {"actions": [{"verb": "add_tag", "tag": "sent"}]}, x=200),
                node("bad", "action", {"actions": [{"verb": "add_tag", "tag": "failed"}]}, x=400),
            ],
            [edge("sms", "default", "ok"), edge("sms", "error", "bad")],
        ),
    )


def sms_connection(workspace: Any, *, external_id: str = "+15550001111") -> ChannelConnection:
    return ChannelConnection.objects.create(
        workspace=workspace,
        platform=Platform.SMS.value,
        display_name=external_id,
        external_id=external_id,
    )


def phone_identity(workspace: Any, contact: Any, connection: Any, address: str = NUMBER) -> Any:
    """A consented SMS identity, as ingest or the CRM would have left one.

    ``opt_in_at`` and ``opt_in_source`` are not decoration: SPEC §11.8's consent
    audit is a database check constraint (``identity_optin_is_audited``), so an
    identity claiming ``opt_in`` without them cannot be stored at all.
    """
    from django.utils import timezone

    from apps.messaging.models import ContactChannelIdentity, OptInSource

    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.SMS.value,
        platform_user_id=address,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source=OptInSource.MESSAGE_IN,
    )


def tags(contact: Any) -> set[str]:
    return {tag.name for tag in contact.tags.all()}


class TestRegistration:
    def test_it_is_registered_and_never_inline(self) -> None:
        """``synchronous_safe`` is a class attribute and L4-A's budget reads it
        through the registry — there is deliberately no second list of safe node
        types (``apps/flows/triggers/safety.py``)."""
        from apps.flows.engine.nodes.send_sms import SendSmsNode

        assert SendSmsNode.synchronous_safe is False
        assert synchronous_safe("send_sms") is False

    def test_it_has_a_schema(self) -> None:
        from apps.flows.schema import node_spec

        spec = node_spec("send_sms")
        assert spec is not None
        assert set(spec.handles) == {"default", "error"}


class TestSending:
    def test_it_sends_on_the_workspaces_sms_connection_not_the_runs(self, tenancy: Any, monkeypatch: Any) -> None:
        """The whole point of the node: a contact who started a flow in a
        Telegram chat and reaches a ``send_sms`` gets a text message."""
        facade = FakeFacade().install(monkeypatch)
        telegram = connection_for(tenancy.workspace)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        execution = start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API, connection=telegram)

        (call,) = facade.named("send_outbound")
        assert call["connection"] == sms
        assert call["source"] == "automation"
        assert execution.status == ExecutionStatus.COMPLETED
        assert tags(contact) == {"sent"}

    def test_placeholders_render_through_the_shared_renderer(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace, first_name="Ada")
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(
            contact,
            sms_flow(tenancy.workspace, {"text": "Hi {{first_name}}, your code is 1234."}),
            started_by=StartedBy.API,
        )

        (call,) = facade.named("send_outbound")
        assert call["outbound"].blocks[0].text == "Hi Ada, your code is 1234."

    def test_a_media_url_becomes_an_image_block(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(
            contact,
            sms_flow(tenancy.workspace, {"text": "Look", "media_url": "https://example.test/a.jpg"}),
            started_by=StartedBy.API,
        )

        (call,) = facade.named("send_outbound")
        kinds = [getattr(block, "kind", "text") for block in call["outbound"].blocks]
        assert kinds == ["text", "image"]

    def test_a_placeholder_in_the_media_url_is_percent_encoded(self, tenancy: Any, monkeypatch: Any) -> None:
        """Twilio fetches this URL server-side, so a contact field substituted
        into it is an injection point unless it is encoded (SECURITY-BASELINE
        §3). ``mode="url"`` encodes the substituted value and leaves the
        template alone, which is how ``external_request`` renders its URL."""
        facade = FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace, first_name="../private/secret")
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(
            contact,
            sms_flow(
                tenancy.workspace,
                {"text": "Look", "media_url": "https://cdn.example/{{first_name}}.jpg"},
            ),
            started_by=StartedBy.API,
        )

        (call,) = facade.named("send_outbound")
        media = next(block for block in call["outbound"].blocks if getattr(block, "kind", "") == "image")
        assert media.url == "https://cdn.example/..%2Fprivate%2Fsecret.jpg"
        assert "/private/secret" not in media.url

    def test_the_idempotency_key_is_specs(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        execution = start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        (call,) = facade.named("send_outbound")
        assert call["idempotency_key"] == f"exec:{execution.pk}:node:sms:0"

    def test_it_sends_on_the_connection_the_identity_is_bound_to(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        first = sms_connection(tenancy.workspace, external_id="+15550001111")
        sms_connection(tenancy.workspace, external_id="+15550002222")
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, first)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound")[0]["connection"] == first

    def test_a_contact_known_only_on_the_newer_number_still_gets_the_message(
        self, tenancy: Any, monkeypatch: Any
    ) -> None:
        """Choosing a connection before looking for the identity meant a contact
        who had only ever texted the *newer* number followed the error edge,
        with a perfectly good active connection and phone identity in the same
        workspace. The two have to be resolved together."""
        facade = FakeFacade().install(monkeypatch)
        sms_connection(tenancy.workspace, external_id="+15550001111")
        newer = sms_connection(tenancy.workspace, external_id="+15550002222")
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, newer)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound")[0]["connection"] == newer
        assert tags(contact) == {"sent"}

    def test_a_bound_identity_beats_a_pending_one(self, tenancy: Any, monkeypatch: Any) -> None:
        """A number the contact has demonstrably used is a better answer than one
        captured before any connection existed."""
        facade = FakeFacade().install(monkeypatch)
        oldest = sms_connection(tenancy.workspace, external_id="+15550001111")
        newer = sms_connection(tenancy.workspace, external_id="+15550002222")
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, None, address="+15557770000")
        phone_identity(tenancy.workspace, contact, newer)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound")[0]["connection"] == newer
        assert facade.named("send_outbound")[0]["connection"] != oldest

    def test_several_bound_identities_resolve_to_the_oldest_connection(self, tenancy: Any, monkeypatch: Any) -> None:
        """Stable rather than row-order dependent."""
        facade = FakeFacade().install(monkeypatch)
        oldest = sms_connection(tenancy.workspace, external_id="+15550001111")
        newer = sms_connection(tenancy.workspace, external_id="+15550002222")
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, newer)
        phone_identity(tenancy.workspace, contact, oldest, address="+15557779999")

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound")[0]["connection"] == oldest

    def test_a_pending_identity_counts(self, tenancy: Any, monkeypatch: Any) -> None:
        """Captured before any SMS connection existed, so ``channel_connection``
        is NULL. Contract 1 upgrades exactly those at first send, onto the
        oldest active connection — which is the one chosen here, so the facade's
        lazy upgrade lands where this node predicted."""
        facade = FakeFacade().install(monkeypatch)
        oldest = sms_connection(tenancy.workspace, external_id="+15550001111")
        sms_connection(tenancy.workspace, external_id="+15550002222")
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, None)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound")[0]["connection"] == oldest


class TestTheErrorHandle:
    def test_no_sms_connection_follows_error(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        contact = contact_for(tenancy.workspace)

        execution = start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound") == []
        assert execution.status == ExecutionStatus.COMPLETED
        assert tags(contact) == {"failed"}

    def test_a_disabled_connection_does_not_count(self, tenancy: Any, monkeypatch: Any) -> None:
        FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        sms.status = ConnectionStatus.DISABLED
        sms.save(update_fields=["status"])
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert tags(contact) == {"failed"}

    def test_no_phone_identity_follows_error(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace, phone="+15557778888")

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound") == []
        assert tags(contact) == {"failed"}

    def test_it_never_fabricates_an_identity_from_contact_phone(self, tenancy: Any, monkeypatch: Any) -> None:
        """A number in a CRM field is not consent to text it. Fabricating one
        here would route past the compliance engine's ``no_opt_in`` rule."""
        facade = FakeFacade().install(monkeypatch)
        sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace, phone=NUMBER)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("upsert_contact_identity") == []
        from apps.messaging.models import ContactChannelIdentity

        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()

    def test_another_workspaces_identity_is_not_reachable(
        self, tenancy: Any, other_tenancy: Any, monkeypatch: Any
    ) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms_connection(tenancy.workspace)
        theirs = sms_connection(other_tenancy.workspace, external_id="+15550009999")
        phone_identity(other_tenancy.workspace, contact_for(other_tenancy.workspace), theirs)
        contact = contact_for(tenancy.workspace)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert facade.named("send_outbound") == []

    def test_a_suppressed_contact_follows_error_rather_than_default(self, tenancy: Any, monkeypatch: Any) -> None:
        """Contract 1 returns a denial as a ``failed`` row rather than raising,
        and SPEC §11.9 gives this node an ``error`` handle to route it down."""
        facade = FakeFacade().install(monkeypatch)
        facade.result = FakeMessage(status="failed", error="opted_out")
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert tags(contact) == {"failed"}

    def test_an_empty_body_follows_error(self, tenancy: Any, monkeypatch: Any) -> None:
        facade = FakeFacade().install(monkeypatch)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        start_flow(contact, sms_flow(tenancy.workspace, {"text": "   "}), started_by=StartedBy.API)

        assert facade.named("send_outbound") == []
        assert tags(contact) == {"failed"}

    def test_a_missing_facade_is_a_named_failure_not_a_silent_skip(self, tenancy: Any, monkeypatch: Any) -> None:
        """A deployment problem rather than a flow problem, and one no retry fixes."""
        from apps.flows import messaging

        monkeypatch.setattr(messaging, "available", lambda: False)
        monkeypatch.setattr(messaging, "_services", lambda: None)
        sms = sms_connection(tenancy.workspace)
        contact = contact_for(tenancy.workspace)
        phone_identity(tenancy.workspace, contact, sms)

        execution = start_flow(contact, sms_flow(tenancy.workspace), started_by=StartedBy.API)

        assert execution.status == ExecutionStatus.FAILED
        assert "send_sms node sms" in execution.last_error
