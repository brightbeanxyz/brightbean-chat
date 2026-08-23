"""WhatsApp end to end: a signed delivery in, a Cloud API call out.

Everything between the two ends is the production path — the webhook endpoint,
signature verification, deduplication, the contract-6 seam, L3-A's persistence,
identity resolution, the compliance chokepoint and the token bucket, and this
adapter. The only substitution is the network (``httpx.MockTransport``).

The compliance cases are the ones the issue lists as acceptance criteria, and
the claim being tested is stronger than "they work": they are decided by
**policy data alone**. ``apps/messaging/`` contains no WhatsApp branch, and
:class:`TestContractFourStaysAdditive` asserts that structurally rather than
trusting the reader.
"""

import tokenize
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.tests.whatsapp_support import (
    PLATFORM_USER_ID,
    app_secret_settings,
    fake_graph_api,
    load_delivery,
    make_connection,
    post_delivery,
)
from apps.messaging.codes import Denial, Grant
from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events
from apps.messaging.models import (
    ContactChannelIdentity,
    Conversation,
    Message,
    MessageDirection,
    MessageStatus,
)
from apps.messaging.services import send_outbound
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

TEXT = OutboundMessage(blocks=(TextBlock(text="Hello"),))
TEMPLATED = OutboundMessage(
    blocks=(TextBlock(text="Hi Ada, order 42 shipped."),),
    template_ref="order_shipped/en_US",
    template_variables=(("body.1", "Ada"), ("body.2", "42")),
)


@pytest.fixture(autouse=True)
def app_secret(settings: Any) -> None:
    """A Meta app secret on the deployment level of the credential chain.

    An ``override_settings`` in ``pytestmark`` is not a pytest mark and pytest
    refuses the module; the ``settings`` fixture is the pytest-django way and it
    composes with the per-test overrides below.
    """
    for key, value in app_secret_settings().items():
        setattr(settings, key, value)


@pytest.fixture(autouse=True)
def persistence() -> Iterator[None]:
    """L3-A's persistence on the contract-6 seam.

    The channels app's conftest empties the seam for every test in it, which is
    right for a parser test and wrong for this one: the whole point here is that
    a delivery becomes a contact, a conversation and a message.
    """
    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    yield


@pytest.fixture
def connection(tenancy: Tenancy) -> ChannelConnection:
    return make_connection(tenancy.workspace)


def identity_for(connection: ChannelConnection) -> ContactChannelIdentity:
    return ContactChannelIdentity.objects.unscoped().get(
        channel_connection=connection, platform_user_id=PLATFORM_USER_ID
    )


def close_the_window(identity: ContactChannelIdentity) -> None:
    """Put the 24-hour window in the past.

    Written here rather than through the facade on purpose: ROADMAP contract 3
    gives ``window_expires_at`` exactly one write site in application code
    (``messaging.ingest``), and the AST scan that enforces it skips tests.
    """
    identity.window_expires_at = timezone.now() - timedelta(hours=1)
    identity.save(update_fields=["window_expires_at", "updated_at"])


class TestInboundLandsAsAThread:
    def test_a_signed_delivery_becomes_a_contact_a_conversation_and_a_message(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        response = post_delivery(client, load_delivery("message_text"))
        assert response.status_code == 200

        identity = identity_for(connection)
        assert identity.contact.first_name == "" or identity.contact.first_name
        conversation = Conversation.objects.for_workspace(tenancy.workspace).get()
        message = Message.objects.for_workspace(tenancy.workspace).get()
        assert message.direction == MessageDirection.IN
        assert message.conversation_id == conversation.pk
        assert message.body["blocks"][0]["text"] == "Hello there"

    def test_the_window_opens_on_the_way_in(self, client: Client, connection: ChannelConnection) -> None:
        """SPEC §8: window bookkeeping happens in the webhook path and nowhere else."""
        post_delivery(client, load_delivery("message_text"))
        expires_at = identity_for(connection).window_expires_at
        assert expires_at is not None
        assert expires_at > timezone.now()

    def test_a_redelivery_is_deduplicated(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        for _ in range(3):
            post_delivery(client, load_delivery("message_text"))
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_a_forged_signature_is_refused_and_nothing_is_persisted(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        response = post_delivery(client, load_delivery("message_text"), secret="not-the-app-secret")
        assert response.status_code == 403
        assert not Message.objects.for_workspace(tenancy.workspace).exists()

    def test_an_unsigned_delivery_is_refused(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        assert post_delivery(client, load_delivery("message_text"), sign=False).status_code == 403
        assert not Message.objects.for_workspace(tenancy.workspace).exists()

    def test_with_no_app_secret_configured_nothing_verifies(
        self, client: Client, connection: ChannelConnection, settings: Any
    ) -> None:
        """Fails closed, and indistinguishably from a wrong signature."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
        assert post_delivery(client, load_delivery("message_text")).status_code == 403

    def test_a_delivery_for_a_number_nobody_connected_is_refused(self, client: Client) -> None:
        """Same 403 as a bad signature: a distinguishable answer would confirm
        which phone number ids this deployment holds."""
        assert post_delivery(client, load_delivery("message_text")).status_code == 403

    def test_a_receipt_moves_the_message_along_the_ladder(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        post_delivery(client, load_delivery("message_text"))
        identity = identity_for(connection)
        with fake_graph_api():
            sent = send_outbound(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key="e2e-1",
            )
        assert sent.status == MessageStatus.SENT

        delivery = load_delivery("status_delivered")
        delivery["entry"][0]["changes"][0]["value"]["statuses"][0]["id"] = sent.provider_message_id
        post_delivery(client, delivery)

        sent.refresh_from_db()
        assert sent.status == MessageStatus.DELIVERED

    def test_a_failed_receipt_lands_a_registered_code_on_the_row(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        post_delivery(client, load_delivery("message_text"))
        identity = identity_for(connection)
        with fake_graph_api():
            sent = send_outbound(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key="e2e-2",
            )

        delivery = load_delivery("status_failed_reengagement")
        delivery["entry"][0]["changes"][0]["value"]["statuses"][0]["id"] = sent.provider_message_id
        post_delivery(client, delivery)

        sent.refresh_from_db()
        assert sent.status == MessageStatus.FAILED
        assert sent.error == Denial.NEEDS_TEMPLATE.value


class TestProfileNameReachesTheIdentity:
    """SPEC §5 gives ``contact_channel_identity.extra`` the job of holding a
    person's display detail, and the issue's scope says "profile name → identity
    extra". The adapter's half is putting it in ``payload.extra``; the other
    half is a platform-agnostic merge in the persistence stage, without which
    the column stayed empty and the inbox showed a bare phone number.
    """

    def test_the_profile_name_is_stored(self, client: Client, connection: ChannelConnection) -> None:
        post_delivery(client, load_delivery("message_text"))
        assert identity_for(connection).extra["profile_name"] == "Ada Lovelace"

    def test_a_later_event_without_it_does_not_erase_it(self, client: Client, connection: ChannelConnection) -> None:
        """Merge, not replace: a delivery carrying no contacts array must not
        blank a name an earlier one supplied."""
        post_delivery(client, load_delivery("message_text"))

        delivery = load_delivery("message_text")
        value = delivery["entry"][0]["changes"][0]["value"]
        del value["contacts"]
        value["messages"][0]["id"] = "wamid.TEXT2"
        post_delivery(client, delivery)

        assert identity_for(connection).extra["profile_name"] == "Ada Lovelace"

    def test_per_event_detail_is_not_copied_onto_the_identity(
        self, client: Client, connection: ChannelConnection
    ) -> None:
        """``payload.extra`` also carries event detail — a reply id, a media
        kind. An allowlist is what keeps the column from becoming a log."""
        post_delivery(client, load_delivery("interactive_button_reply"))
        assert "reply_id" not in identity_for(connection).extra

    def test_a_renamed_contact_is_updated(self, client: Client, connection: ChannelConnection) -> None:
        post_delivery(client, load_delivery("message_text"))

        delivery = load_delivery("message_text")
        value = delivery["entry"][0]["changes"][0]["value"]
        value["contacts"][0]["profile"]["name"] = "Ada Byron"
        value["messages"][0]["id"] = "wamid.TEXT3"
        post_delivery(client, delivery)

        assert identity_for(connection).extra["profile_name"] == "Ada Byron"


class TestIdentityLinking:
    def test_a_wa_id_links_to_a_contact_captured_by_phone(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        """``apps.messaging.identities`` named this issue as the one that adds
        WhatsApp to ``ADDRESS_PLATFORMS``; this is the behaviour that buys."""
        from apps.contacts.services import create_contact

        existing = create_contact(workspace=tenancy.workspace, first_name="Ada", phone=PLATFORM_USER_ID)
        post_delivery(client, load_delivery("message_text"))
        assert identity_for(connection).contact_id == existing.pk

    def test_a_different_number_does_not_link(
        self, client: Client, connection: ChannelConnection, tenancy: Tenancy
    ) -> None:
        from apps.contacts.services import create_contact

        create_contact(workspace=tenancy.workspace, first_name="Someone", phone="+15550009999")
        post_delivery(client, load_delivery("message_text"))
        assert identity_for(connection).contact.first_name != "Someone"


class TestComplianceFromPolicyDataAlone:
    """SPEC §8's WhatsApp row, exercised through the real send pipeline."""

    @pytest.fixture
    def identity(self, client: Client, connection: ChannelConnection) -> ContactChannelIdentity:
        post_delivery(client, load_delivery("message_text"))
        return identity_for(connection)

    def send(
        self, tenancy: Tenancy, connection: Any, identity: Any, outbound: Any, key: str, source: str = "automation"
    ) -> Message:
        with fake_graph_api():
            return send_outbound(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=connection,
                outbound=outbound,
                source=source,
                idempotency_key=key,
            )

    def test_in_window_a_session_message_goes(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        message = self.send(tenancy, connection, identity, TEXT, "in-window")
        assert message.status == MessageStatus.SENT

    def test_outside_the_window_a_session_message_fails_with_needs_template(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        """SPEC §8: "flow sends outside window fail the node with a logged error
        (do not silently drop)". The row is the record of that."""
        close_the_window(identity)
        message = self.send(tenancy, connection, identity, TEXT, "outside-plain")
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NEEDS_TEMPLATE.value

    def test_outside_the_window_a_template_goes(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        close_the_window(identity)
        message = self.send(tenancy, connection, identity, TEMPLATED, "outside-template")
        assert message.status == MessageStatus.SENT
        assert message.body["template_ref"] == "order_shipped/en_US"

    def test_a_template_send_puts_a_template_payload_on_the_wire(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        close_the_window(identity)
        with fake_graph_api() as fake:
            send_outbound(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=connection,
                outbound=TEMPLATED,
                source="automation",
                idempotency_key="outside-wire",
            )
        (payload,) = fake.payloads("messages")
        assert payload["type"] == "template"
        assert payload["template"]["name"] == "order_shipped"

    def test_an_agent_outside_the_window_gets_no_human_agent_escape(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        """``human_agent_days=None`` in the policy row. An agent outside the
        window sends a template too."""
        close_the_window(identity)
        message = self.send(tenancy, connection, identity, TEXT, "agent-outside", source="agent")
        assert message.error == Denial.NEEDS_TEMPLATE.value

    def test_an_opt_out_beats_a_template(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        """SPEC §19's first rule, on every platform, before anything else."""
        from apps.messaging.services import record_opt_out

        record_opt_out(identity)
        message = self.send(tenancy, connection, identity, TEMPLATED, "opted-out")
        assert message.error == Denial.OPTED_OUT.value

    def test_the_grant_says_which_rule_let_it_through(
        self, tenancy: Tenancy, connection: ChannelConnection, identity: ContactChannelIdentity
    ) -> None:
        from apps.channels.policy import policy_for
        from apps.common.platforms import Platform
        from apps.messaging.compliance import Allowed, can_send

        assert policy_for(Platform.WHATSAPP).outside_window == "needs_template"
        close_the_window(identity)
        decision = can_send(identity, "automation", TEMPLATED)
        assert isinstance(decision, Allowed)
        assert decision.code == Grant.TEMPLATE_SUPPLIED.value


class TestContractFourStaysAdditive:
    """No WhatsApp branch anywhere in ``apps/messaging/`` (ROADMAP contract 4).

    Asserted over tokens rather than by grepping text, because the answer that
    matters is "does any *code* mention this platform" and a docstring saying
    the word is not a branch — ``apps/messaging`` has several, deliberately.
    """

    #: The one place the platform may be named in code: a data table saying which
    #: platforms carry a real-world address, exactly like the SMS and email rows
    #: beside it. ``apps.messaging.identities``'s own docstring asked issue #19
    #: to add it.
    ALLOWED = {("identities.py", 1)}

    def test_the_only_code_mention_is_the_address_table(self) -> None:
        found: set[tuple[str, int]] = set()
        root = Path(__file__).resolve().parents[3] / "apps" / "messaging"
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            count = _code_mentions(path, "whatsapp")
            if count:
                found.add((path.name, count))
        assert found == self.ALLOWED


def _code_mentions(path: Path, needle: str) -> int:
    """How often ``needle`` appears in ``path``'s code, ignoring strings and comments."""
    total = 0
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            total += token.string.lower().count(needle)
    return total
