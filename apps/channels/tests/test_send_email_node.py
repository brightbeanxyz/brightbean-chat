"""The ``send_email`` node (SPEC §11.10).

Two things this file is really about:

* the **handle split** — a missing prerequisite takes ``error``, a failed send
  takes ``default`` — because getting it backwards would send flows down an
  error branch every time somebody unsubscribed;
* the **escaping**, which is the acceptance criterion "HTML injection via
  ``{{first_name}}`` renders escaped in body".
"""

from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import TextBlock
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers import email_backends
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.flows.engine.registry import node_class_for, synchronous_safe
from apps.flows.engine.results import Continue
from apps.flows.tests.support import graph, node, published_flow
from apps.messaging.models import ContactChannelIdentity, Message, MessageStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def email_connection(tenancy: Any) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    connection.credentials = {  # type: ignore[assignment]
        "provider": "smtp",
        "host": "mail.test",
        "security": "none",
        "from_address": "hello@sender.test",
        "from_name": "Sender",
    }
    connection.save()
    return connection


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every envelope that reached a backend, with the wire replaced."""
    envelopes: list[Any] = []

    def record(connection: Any, envelope: Any) -> str:
        envelopes.append(envelope)
        return "id-1"

    monkeypatch.setattr(email_backends, "deliver", record)
    return envelopes


def run_node(
    tenancy: Any,
    contact: Any,
    config: dict[str, Any],
    connection: Any = None,
    *,
    runtime_config: dict[str, Any] | None = None,
) -> Any:
    """Execute one ``send_email`` node against a real execution row.

    ``runtime_config`` is for the shapes the *schema* refuses — an empty body,
    say. Those can only reach a runtime through a hand-edited ``graph_json``, so
    the published flow carries a legal config and the node is handed the other
    one, rather than the test asking the validator to accept something it
    correctly rejects.
    """
    from apps.flows.engine.context import NodeContext
    from apps.flows.engine.graph import Graph
    from apps.flows.models import FlowExecution

    flow = published_flow(tenancy.workspace, graph([node("n1", "send_email", config)]))
    version = flow.versions.get(published=True)
    execution = FlowExecution.objects.create(
        workspace=tenancy.workspace,
        flow_version=version,
        contact=contact,
        channel_connection=connection,
        current_node_id="n1",
        status="running",
    )
    context = NodeContext(
        execution=execution,
        graph=Graph(version.graph_json),
        node_id="n1",
        node_type="send_email",
        config=config if runtime_config is None else runtime_config,
        variables={},
    )
    return _runtime().execute(context), execution


def _runtime() -> Any:
    """The registered ``send_email`` runtime, asserted present."""
    node_class = node_class_for("send_email")
    assert node_class is not None
    return node_class()


def identity_for(tenancy: Any, connection: ChannelConnection, contact: Any, **extra: Any) -> ContactChannelIdentity:
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.EMAIL.value,
        platform_user_id=contact.email,
        opt_in=extra.pop("opt_in", True),
        opt_in_at=timezone.now(),
        opt_in_source="data_collection",
        **extra,
    )


CONFIG = {"subject": "Hello {{first_name}}", "html_body": "<p>Hi {{first_name}}</p>"}


class TestRegistration:
    def test_the_runtime_is_registered(self) -> None:
        assert node_class_for("send_email") is not None

    def test_it_is_not_synchronous_safe(self) -> None:
        """SPEC §7.1's safe five do not include it: SMTP is seconds of wall clock."""
        assert synchronous_safe("send_email") is False


class TestPrerequisites:
    def test_no_email_connection_takes_the_error_handle(self, tenancy: Any) -> None:
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        result, _ = run_node(tenancy, contact, CONFIG)
        assert result == Continue("error")

    def test_a_disabled_connection_does_not_count(self, tenancy: Any, email_connection: ChannelConnection) -> None:
        email_connection.status = ConnectionStatus.DISABLED
        email_connection.save(update_fields=["status", "updated_at"])
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        result, _ = run_node(tenancy, contact, CONFIG)
        assert result == Continue("error")

    def test_a_contact_with_no_address_takes_the_error_handle(
        self, tenancy: Any, email_connection: ChannelConnection
    ) -> None:
        contact = create_contact(tenancy.workspace, source="manual")
        result, _ = run_node(tenancy, contact, CONFIG)
        assert result == Continue("error")

    def test_an_identity_on_another_email_connection_is_not_mistaken_for_one(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """The node's check and the facade's resolution have to agree.

        An unscoped "any email identity for this contact" check said yes for an
        identity belonging to a *different* email connection, so the node
        proceeded and `send_outbound` then failed the message with `no_identity`
        — reporting a missing prerequisite down `default` instead of `error`.
        """
        newer = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Second sender",
            external_id="second.test",
        )
        newer.credentials = {"provider": "smtp", "host": "mail.test", "from_address": "hi@second.test"}  # type: ignore[assignment]
        newer.save()

        # No `contact.email`, so there is no address to fall back to and the
        # only identity in play is the one on the wrong connection.
        contact = create_contact(tenancy.workspace, source="manual")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=newer,
            platform=Platform.EMAIL.value,
            platform_user_id="reader@example.test",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="data_collection",
        )

        result, _ = run_node(tenancy, contact, CONFIG)

        assert result == Continue("error")
        assert sent == []

    def test_a_pending_identity_counts(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """A connection-less row is upgraded by the facade at first send."""
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=None,
            platform=Platform.EMAIL.value,
            platform_user_id="reader@example.test",
            opt_in=True,
            opt_in_at=timezone.now(),
            opt_in_source="data_collection",
        )

        result, _ = run_node(tenancy, contact, CONFIG)

        assert result == Continue("default")
        assert len(sent) == 1

    def test_the_node_runs_on_a_connectionless_execution(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """The reason it does not use ``sending.deliver``.

        An email node fires inside a run that started on Telegram, or on nothing
        at all, and the email still has to go out over the email connection.
        """
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        result, _ = run_node(tenancy, contact, CONFIG, connection=None)

        assert result == Continue("default")
        assert sent[0].to == "reader@example.test"


class TestConsent:
    def test_an_address_off_the_contact_record_is_captured_without_opt_in(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """``apps/contacts/imports.py``'s decision, one app over.

        Holding somebody's address is not permission to email them, so the
        identity is created — with its audit trail — and compliance refuses.
        """
        contact = create_contact(tenancy.workspace, source="import", email="imported@example.test")

        result, execution = run_node(tenancy, contact, CONFIG)

        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get(contact=contact)
        assert identity.opt_in is False
        assert identity.platform_user_id == "imported@example.test"
        # A refused send, not a missing prerequisite: the row says why.
        assert result == Continue("default")
        assert sent == []
        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.status == MessageStatus.FAILED
        assert message.error == "no_opt_in"

    def test_a_consented_identity_sends(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        result, _ = run_node(tenancy, contact, CONFIG)

        assert result == Continue("default")
        assert len(sent) == 1
        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.status == MessageStatus.SENT


class TestFailedSendsFollowDefault:
    """SPEC §9.5: the message failed, the flow did not."""

    def test_an_opted_out_identity_does_not_divert_the_flow(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact, opted_out_at=timezone.now())

        result, _ = run_node(tenancy, contact, CONFIG)

        assert result == Continue("default")
        assert sent == []
        assert Message.objects.for_workspace(tenancy.workspace).get().error == "opted_out"

    def test_an_empty_body_is_skipped_rather_than_sent(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """Only reachable from a hand-edited graph: the schema requires a body."""
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        result, _ = run_node(tenancy, contact, CONFIG, runtime_config={"subject": "Hi", "html_body": ""})

        assert result == Continue("default")
        assert sent == []


class TestRendering:
    def test_placeholders_are_substituted(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test", first_name="Ada")
        identity_for(tenancy, email_connection, contact)

        run_node(tenancy, contact, CONFIG)

        assert sent[0].subject == "Hello Ada"
        assert "Hi Ada" in sent[0].html

    def test_html_in_a_placeholder_value_renders_escaped(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """The acceptance criterion, asserted on what reaches the backend.

        The author's markup stays markup and the *contact's* does not — which is
        the asymmetry ``apps/flows/rendering.py`` exists to enforce.
        """
        contact = create_contact(
            tenancy.workspace,
            source="manual",
            email="reader@example.test",
            first_name="<script>alert(1)</script>",
        )
        identity_for(tenancy, email_connection, contact)

        run_node(tenancy, contact, CONFIG)

        assert "<script>" not in sent[0].html
        assert "&lt;script&gt;" in sent[0].html
        # The author's own paragraph survived, so this is escaping and not
        # blanket sanitising.
        assert "<p>" in sent[0].html

    def test_the_subject_is_plain_text_not_html(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """SPEC §19: "auto-escaping in email HTML context; plain text elsewhere"."""
        contact = create_contact(
            tenancy.workspace, source="manual", email="reader@example.test", first_name="Ada & Bob"
        )
        identity_for(tenancy, email_connection, contact)

        run_node(tenancy, contact, CONFIG)

        # A header is not an HTML context, so the ampersand stays an ampersand.
        assert sent[0].subject == "Hello Ada & Bob"

    def test_a_from_override_on_the_sending_domain_reaches_the_envelope(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        run_node(tenancy, contact, {**CONFIG, "from_override": "billing@sender.test"})

        assert sent[0].from_address == "billing@sender.test"

    def test_a_from_override_cannot_leave_the_sending_domain(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """Node config is written with `edit_flows`; the From address is `manage_channels`."""
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        run_node(tenancy, contact, {**CONFIG, "from_override": "ceo@bank.test"})

        assert sent[0].from_address == "hello@sender.test"

    def test_a_long_body_is_not_truncated_at_the_chat_limit(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """The renderer's 20 000-character default would cut an email mid-tag.

        ``html_body``'s schema allows 100 000 and so does email's
        ``max_text_len``, so the node passes the destination's own limit.
        """
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)
        body = "<p>" + ("word " * 6000) + "</p>"
        assert len(body) > 20_000

        run_node(tenancy, contact, {"subject": "Long", "html_body": body})

        assert len(sent[0].html) > 20_000
        assert sent[0].html.rstrip().endswith("</p>") or "</p>" in sent[0].html


class TestAddressLimits:
    def test_an_address_too_long_to_store_takes_the_error_handle(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """`bounded_key` hashes rather than truncates, so storing it makes a non-address.

        The send would then fail with an opaque `no_address` every time. Better
        to say so once, on a handle the flow author can branch on.
        """
        long_address = ("x" * 240) + "@example.test"
        contact = create_contact(tenancy.workspace, source="import", email=long_address)

        result, _ = run_node(tenancy, contact, CONFIG)

        assert result == Continue("error")
        assert sent == []
        assert not ContactChannelIdentity.objects.for_workspace(tenancy.workspace).exists()


class TestGenericSendsGetASubject:
    """Only `send_email` has a subject in its config; everything else does not."""

    def test_an_inbox_reply_is_not_refused_for_having_no_subject(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        from apps.channels.events import OutboundMessage as Outbound
        from apps.channels.providers.email import EmailAdapter

        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity = identity_for(tenancy, email_connection, contact)
        # Exactly what `send_as_agent` builds: blocks, and nothing else.
        reply = Outbound(blocks=(TextBlock(text="Thanks, looking into it."),))

        result = EmailAdapter().send(email_connection, identity, reply)

        assert result.status == "sent"
        assert sent[0].subject == "Message from Sender"

    def test_a_connection_with_no_from_name_still_gets_one(self, tenancy: Any, sent: list[Any]) -> None:
        from apps.channels.events import OutboundMessage as Outbound
        from apps.channels.providers.email import DEFAULT_SUBJECT, EmailAdapter

        bare = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="Bare",
            external_id="bare.test",
        )
        bare.credentials = {"provider": "smtp", "host": "mail.test", "from_address": "hi@bare.test"}  # type: ignore[assignment]
        bare.save()
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity = identity_for(tenancy, bare, contact)

        EmailAdapter().send(bare, identity, Outbound(blocks=(TextBlock(text="Hello"),)))

        assert sent[0].subject == DEFAULT_SUBJECT


class TestIdempotency:
    def test_two_runs_of_the_same_node_make_one_message(
        self, tenancy: Any, email_connection: ChannelConnection, sent: list[Any]
    ) -> None:
        """SPEC §9.4's key, minted by the facade rather than by this node."""
        contact = create_contact(tenancy.workspace, source="manual", email="reader@example.test")
        identity_for(tenancy, email_connection, contact)

        from apps.flows.engine.context import NodeContext
        from apps.flows.engine.graph import Graph
        from apps.flows.models import FlowExecution

        flow = published_flow(tenancy.workspace, graph([node("n1", "send_email", CONFIG)]))
        version = flow.versions.get(published=True)
        execution = FlowExecution.objects.create(
            workspace=tenancy.workspace,
            flow_version=version,
            contact=contact,
            current_node_id="n1",
            status="running",
        )
        context = NodeContext(
            execution=execution,
            graph=Graph(version.graph_json),
            node_id="n1",
            node_type="send_email",
            config=CONFIG,
            variables={},
        )
        runtime = _runtime()
        runtime.execute(context)
        runtime.execute(context)

        assert Message.objects.for_workspace(tenancy.workspace).count() == 1
        assert len(sent) == 1
