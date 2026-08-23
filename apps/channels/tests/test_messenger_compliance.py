"""Message tags, end to end and driven entirely from registry data (SPEC §§6.4, 8).

    Outside-window automation without tag → NeedsTag failure recorded; valid
    non-promotional tag → MESSAGE_TAG payload; HUMAN_AGENT rejected for
    automation source (hard-coded, §22) — all driven from registry data.

The last clause is the point of this module, and it is why almost nothing below
is a literal. ROADMAP contract 4 promises that a platform costs "one module and
one registry line", which is only true if the *rules* live in
``apps.channels.policy`` and the compliance engine reads them as data. A test that
restated SPEC §8's Messenger rules would keep passing while the policy row said
something else — so the expectations here are read from ``policy_for`` and
``capabilities_for``, and what is asserted is that the shipped behaviour follows
them.

``grep -rn messenger apps/messaging/`` is the other half of the same claim, and
``TestNoPlatformBranches`` below is where it is written down.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.capabilities import capabilities_for
from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection
from apps.channels.policy import NeedsTag, policy_for
from apps.channels.tests.messenger_support import PSID, fake_graph
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import services
from apps.messaging.codes import Denial, Grant
from apps.messaging.compliance import HUMAN_AGENT_TAG, can_send
from apps.messaging.models import ContactChannelIdentity, MessageSource, MessageStatus
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

POLICY = policy_for(Platform.MESSENGER.value)
CAPS = capabilities_for(Platform.MESSENGER.value)
TEXT = OutboundMessage(blocks=(TextBlock(text="Your order shipped."),))


def outside_window_tags() -> tuple[str, ...]:
    """The tags the *policy row* says may be used outside the window."""
    assert isinstance(POLICY.outside_window, NeedsTag)
    return POLICY.outside_window.tags


@pytest.fixture
def contact(tenancy: Tenancy) -> Any:
    return create_contact(tenancy.workspace, first_name="Sam")


def identity_for(contact: Any, page: ChannelConnection, *, window_open: bool, last_inbound_days: float) -> Any:
    now = timezone.now()
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=page,
        platform=page.platform,
        platform_user_id=PSID,
        opt_in=True,
        opt_in_at=now,
        opt_in_source="message_in",
        window_expires_at=now + timedelta(hours=1 if window_open else -1),
        last_inbound_at=now - timedelta(days=last_inbound_days),
    )


def send(
    tenancy: Tenancy,
    contact: Any,
    page: ChannelConnection,
    outbound: OutboundMessage,
    *,
    source: str,
    key: str,
) -> tuple[Any, Any]:
    """Push one message through contract 1's entry point. Returns (row, graph)."""
    with fake_graph() as graph:
        message = services.send_outbound(
            workspace=tenancy.workspace,
            contact=contact,
            connection=page,
            outbound=outbound,
            source=source,
            idempotency_key=key,
        )
    return message, graph


class TestThePolicyRowIsTheSpec:
    """SPEC §6.4's numbers, asserted against the row rather than against prose."""

    def test_the_window_and_the_human_agent_extension(self) -> None:
        assert POLICY.window_hours == 24
        assert POLICY.human_agent_days == 7
        assert POLICY.broadcast_allowed is True

    def test_the_three_non_promotional_tags_and_metas_allowed_use_text(self) -> None:
        outside = POLICY.outside_window
        assert isinstance(outside, NeedsTag)
        assert set(outside.tags) == {"CONFIRMED_EVENT_UPDATE", "POST_PURCHASE_UPDATE", "ACCOUNT_UPDATE"}
        # SPEC §6.4 requires the composer to display Meta's own wording verbatim,
        # which is why it is carried with the tag list rather than in a template.
        assert "Non-promotional only" in outside.allowed_use_text

    def test_human_agent_is_not_an_outside_window_tag(self) -> None:
        """It is an *agent* escape, not something automation may ask for.

        The capability table lists it because Meta accepts it on the wire; the
        policy's ``outside_window`` does not, because SPEC §22 makes it available
        "only to inbox sends, never automation, hard-coded".
        """
        assert HUMAN_AGENT_TAG in CAPS.tags_supported
        assert HUMAN_AGENT_TAG not in outside_window_tags()


class TestInsideTheWindow:
    def test_an_untagged_send_goes_out_as_a_response(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        identity_for(contact, page, window_open=True, last_inbound_days=0)
        message, graph = send(tenancy, contact, page, TEXT, source=MessageSource.AUTOMATION, key="k1")
        assert message.status == MessageStatus.SENT
        assert graph.bodies("/messages")[0]["messaging_type"] == "RESPONSE"
        assert "tag" not in graph.bodies("/messages")[0]


class TestOutsideTheWindow:
    def test_automation_with_no_tag_is_recorded_as_needing_one(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """Never silently dropped: the flow engine needs a row to follow ``default`` from."""
        identity_for(contact, page, window_open=False, last_inbound_days=0)
        message, graph = send(tenancy, contact, page, TEXT, source=MessageSource.AUTOMATION, key="k2")
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NEEDS_TAG.value
        assert graph.calls == []

    def test_the_verdict_carries_the_tags_and_metas_text_for_the_composer(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """SPEC §6.4: L6-B's composer forces a tag and shows the allowed-use text.

        It gets both from the decision, so there is no composer-side platform
        logic to write — which is the property this asserts.
        """
        from apps.messaging.compliance import NeedsTag as NeedsTagDecision

        identity = identity_for(contact, page, window_open=False, last_inbound_days=0)
        decision = can_send(identity, MessageSource.AUTOMATION.value, TEXT)
        assert isinstance(decision, NeedsTagDecision)
        assert decision.allowed_tags == outside_window_tags()
        assert decision.allowed_use_text == POLICY.outside_window.allowed_use_text  # type: ignore[union-attr]

    @pytest.mark.parametrize("tag", sorted(outside_window_tags()))
    def test_a_valid_non_promotional_tag_becomes_a_message_tag_send(
        self, tag: str, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        identity_for(contact, page, window_open=False, last_inbound_days=0)
        tagged = OutboundMessage(blocks=(TextBlock(text="Your order shipped."),), tag=tag)
        message, graph = send(tenancy, contact, page, tagged, source=MessageSource.AUTOMATION, key=f"k-{tag}")
        assert message.status == MessageStatus.SENT
        body = graph.bodies("/messages")[0]
        assert body["messaging_type"] == "MESSAGE_TAG"
        assert body["tag"] == tag

    def test_a_tag_outside_the_allowed_set_is_refused_before_the_platform_sees_it(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """Meta restricts pages over exactly this, so it fails closed twice.

        The compliance engine refuses it (the tag is not in the policy row), and
        the adapter would refuse it again (the tag is not in the capability
        table). Either alone would be enough; neither is where a promotional
        broadcast should first be noticed.
        """
        invented = OutboundMessage(blocks=(TextBlock(text="50% off!"),), tag="PROMOTION")
        identity_for(contact, page, window_open=False, last_inbound_days=0)
        message, graph = send(tenancy, contact, page, invented, source=MessageSource.AUTOMATION, key="k3")
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NEEDS_TAG.value
        assert graph.calls == []


class TestHumanAgent:
    def test_an_agent_send_within_seven_days_goes_out_tagged(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        identity_for(contact, page, window_open=False, last_inbound_days=3)
        message, graph = send(tenancy, contact, page, TEXT, source=MessageSource.AGENT, key="k4")
        assert message.status == MessageStatus.SENT
        assert graph.bodies("/messages")[0]["tag"] == HUMAN_AGENT_TAG

    def test_an_agent_send_past_seven_days_needs_an_ordinary_tag(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        identity_for(contact, page, window_open=False, last_inbound_days=30)
        message, graph = send(tenancy, contact, page, TEXT, source=MessageSource.AGENT, key="k5")
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NEEDS_TAG.value
        assert graph.calls == []

    def test_automation_cannot_buy_the_seven_day_escape_by_asking_for_it(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """SPEC §22, hard-coded — and this is the test that keeps it hard-coded.

        ``compliance.Allowed.apply`` **replaces** ``outbound.tag`` rather than
        passing the caller's through, so a flow author who sets HUMAN_AGENT on an
        automation node gets the automation answer. Without that replacement, this
        message would go out under an agent-only allowance.
        """
        identity_for(contact, page, window_open=False, last_inbound_days=1)
        cheating = OutboundMessage(blocks=(TextBlock(text="Hi again"),), tag=HUMAN_AGENT_TAG)
        message, graph = send(tenancy, contact, page, cheating, source=MessageSource.AUTOMATION, key="k6")
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.NEEDS_TAG.value
        assert graph.calls == []

    def test_the_same_send_from_an_agent_is_allowed(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """The mirror of the case above: it is the *source* that decides."""
        identity = identity_for(contact, page, window_open=False, last_inbound_days=1)
        decision = can_send(identity, MessageSource.AGENT.value, TEXT)
        assert getattr(decision, "code", "") == Grant.HUMAN_AGENT.value
        assert getattr(decision, "tag", "") == HUMAN_AGENT_TAG


class TestBroadcasts:
    def test_a_broadcast_outside_the_window_needs_a_tag_like_any_automation(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        identity_for(contact, page, window_open=False, last_inbound_days=0)
        message, _graph = send(tenancy, contact, page, TEXT, source=MessageSource.BROADCAST, key="k7")
        assert message.error == Denial.NEEDS_TAG.value

    def test_a_tagged_broadcast_is_allowed_because_the_policy_says_so(
        self, tenancy: Tenancy, contact: Any, page: ChannelConnection
    ) -> None:
        """``broadcast_allowed=True`` is the Messenger row's, and Instagram's is False."""
        identity_for(contact, page, window_open=False, last_inbound_days=0)
        tagged = OutboundMessage(blocks=(TextBlock(text="Your event is tomorrow."),), tag="CONFIRMED_EVENT_UPDATE")
        message, graph = send(tenancy, contact, page, tagged, source=MessageSource.BROADCAST, key="k8")
        assert message.status == MessageStatus.SENT
        assert graph.bodies("/messages")[0]["tag"] == "CONFIRMED_EVENT_UPDATE"


class TestNoPlatformBranches:
    """ROADMAP contract 4, checked rather than promised."""

    def test_apps_messaging_names_no_platform(self) -> None:
        """The standard Telegram set: a migration's choices list and a docstring.

        Written as a test rather than as a line in the PR description because a
        branch added later would otherwise be caught by nobody — this is the
        property the whole policy-as-data design exists to keep.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[3] / "apps" / "messaging"
        offenders = []
        for path in root.rglob("*.py"):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if re.search(r"\bmessenger\b", line, re.IGNORECASE):
                    offenders.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
        assert offenders == []
