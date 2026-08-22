"""The compliance engine (SPEC §8) — the product's one send chokepoint.

Two shapes of test, on purpose. A **golden table** for the six platforms that
exist, reviewable by eye against SPEC §8's prose; and **property assertions
derived from the policy fields**, so a Layer-5 platform that adds a row to
``apps.channels.policy.POLICIES`` enters the matrix without anybody editing this
file. That is what "driven from registry data" has to mean to be worth
anything.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.channels import policy as channel_policy
from apps.channels.events import OutboundMessage, TextBlock
from apps.common.platforms import Platform
from apps.messaging.codes import Denial, Grant
from apps.messaging.compliance import (
    HUMAN_AGENT_TAG,
    Allowed,
    Blocked,
    NeedsTag,
    NeedsTemplate,
    can_send,
)
from apps.messaging.models import ContactChannelIdentity, MessageSource

ALL_PLATFORMS = sorted(channel_policy.POLICIES)
ALL_SOURCES = [source.value for source in MessageSource]
AUTOMATION_SOURCES = [s for s in ALL_SOURCES if s not in (MessageSource.AGENT, MessageSource.BROADCAST)]

TEXT = OutboundMessage(blocks=(TextBlock(text="hello"),))
NOW = timezone.now()


def identity(
    platform: str,
    *,
    opt_in: bool = True,
    opted_out: bool = False,
    window_open: bool | None = True,
    last_inbound_days: float | None = 0.0,
    connection: bool = True,
) -> ContactChannelIdentity:
    """An unsaved identity in a named state. No database: can_send is pure."""
    window = None
    if window_open is not None:
        window = NOW + timedelta(hours=1) if window_open else NOW - timedelta(hours=1)
    return ContactChannelIdentity(
        platform=platform,
        platform_user_id="u1",
        opt_in=opt_in,
        opt_in_at=NOW if opt_in else None,
        opted_out_at=NOW if opted_out else None,
        window_expires_at=window,
        last_inbound_at=None if last_inbound_days is None else NOW - timedelta(days=last_inbound_days),
        channel_connection_id="a" if connection else None,
    )


def decide(ident: ContactChannelIdentity, source: str, outbound: OutboundMessage = TEXT) -> Any:
    return can_send(ident, source, outbound, now=NOW)


class TestOptOutBeatsEverything:
    """SPEC §19: enforced here so it cannot be bypassed."""

    @pytest.mark.parametrize("platform", ALL_PLATFORMS)
    @pytest.mark.parametrize("source", ALL_SOURCES)
    def test_an_opted_out_identity_is_blocked_on_every_platform_and_source(self, platform: str, source: str) -> None:
        result = decide(identity(platform, opted_out=True), source)
        assert isinstance(result, Blocked)
        assert result.code == Denial.OPTED_OUT

    @pytest.mark.parametrize("platform", ALL_PLATFORMS)
    def test_not_even_a_tag_or_a_template_gets_round_it(self, platform: str) -> None:
        armed = OutboundMessage(tag="ACCOUNT_UPDATE", template_ref="tpl-1")
        assert isinstance(decide(identity(platform, opted_out=True), "agent", armed), Blocked)


class TestConsentIsRequired:
    @pytest.mark.parametrize("platform", ALL_PLATFORMS)
    def test_an_address_with_no_recorded_consent_is_blocked(self, platform: str) -> None:
        """SPEC §6.2 says to enforce opt_in on the identity; this generalises it
        rather than making it one platform's branch. An inbound message sets
        opt_in, so it only bites addresses captured by import or API."""
        result = decide(identity(platform, opt_in=False), "automation")
        assert isinstance(result, Blocked)
        assert result.code == Denial.NO_OPT_IN

    @pytest.mark.parametrize("platform", ALL_PLATFORMS)
    def test_a_pending_identity_has_nothing_to_send_through(self, platform: str) -> None:
        result = decide(identity(platform, connection=False), "automation")
        assert isinstance(result, Blocked)
        assert result.code == Denial.NO_CONNECTION


class TestWindowlessPlatforms:
    @pytest.mark.parametrize("platform", [p for p in ALL_PLATFORMS if not channel_policy.POLICIES[p].has_window()])
    @pytest.mark.parametrize("source", ALL_SOURCES)
    def test_every_source_is_allowed(self, platform: str, source: str) -> None:
        """has_window() is consulted first. outside_window is populated for
        these platforms and unreachable; reading it would refuse every send."""
        result = decide(identity(platform, window_open=None), source)
        assert isinstance(result, Allowed)
        assert result.code == Grant.NO_WINDOW
        assert result.tag is None


class TestAnOpenWindow:
    @pytest.mark.parametrize("platform", [p for p in ALL_PLATFORMS if channel_policy.POLICIES[p].has_window()])
    @pytest.mark.parametrize("source", ALL_SOURCES)
    def test_every_source_is_allowed_while_the_window_is_open(self, platform: str, source: str) -> None:
        if source == MessageSource.BROADCAST and not channel_policy.POLICIES[platform].broadcast_allowed:
            pytest.skip("broadcast is refused before the window is consulted")
        result = decide(identity(platform, window_open=True), source)
        assert isinstance(result, Allowed)
        assert result.code == Grant.IN_WINDOW

    @pytest.mark.parametrize("platform", [p for p in ALL_PLATFORMS if channel_policy.POLICIES[p].has_window()])
    def test_a_window_that_never_opened_reads_as_closed(self, platform: str) -> None:
        """NULL is the direction to fail in."""
        assert not isinstance(decide(identity(platform, window_open=None), "automation"), Allowed)


class TestOutsideTheWindow:
    """The golden table, from SPEC §8's own prose."""

    def test_instagram_blocks_automation(self) -> None:
        result = decide(identity(Platform.INSTAGRAM, window_open=False), "automation")
        assert isinstance(result, Blocked)
        assert result.code == Denial.OUTSIDE_WINDOW

    def test_instagram_allows_an_agent_within_seven_days(self) -> None:
        result = decide(identity(Platform.INSTAGRAM, window_open=False, last_inbound_days=6), "agent")
        assert isinstance(result, Allowed)
        assert result.code == Grant.HUMAN_AGENT
        assert result.tag == HUMAN_AGENT_TAG

    def test_instagram_blocks_an_agent_beyond_seven_days(self) -> None:
        result = decide(identity(Platform.INSTAGRAM, window_open=False, last_inbound_days=8), "agent")
        assert isinstance(result, Blocked)

    def test_messenger_asks_automation_for_a_tag(self) -> None:
        result = decide(identity(Platform.MESSENGER, window_open=False), "automation")
        assert isinstance(result, NeedsTag)
        assert "ACCOUNT_UPDATE" in result.allowed_tags
        # SPEC §6.4 requires the composer to display Meta's own text verbatim.
        assert "Non-promotional only" in result.allowed_use_text

    def test_messenger_accepts_a_valid_tag(self) -> None:
        tagged = OutboundMessage(tag="ACCOUNT_UPDATE")
        result = decide(identity(Platform.MESSENGER, window_open=False), "automation", tagged)
        assert isinstance(result, Allowed)
        assert result.code == Grant.TAG_SUPPLIED
        assert result.tag == "ACCOUNT_UPDATE"

    def test_messenger_refuses_a_tag_outside_the_allowed_set(self) -> None:
        """Meta disables pages over exactly this, so an unknown tag is refused
        rather than quietly passed through."""
        invented = OutboundMessage(tag="MARKETING_BLAST")
        assert isinstance(decide(identity(Platform.MESSENGER, window_open=False), "automation", invented), NeedsTag)

    def test_whatsapp_asks_for_a_template(self) -> None:
        result = decide(identity(Platform.WHATSAPP, window_open=False), "automation")
        assert isinstance(result, NeedsTemplate)
        assert result.code == Denial.NEEDS_TEMPLATE

    def test_whatsapp_accepts_a_template(self) -> None:
        templated = OutboundMessage(template_ref="tpl-1")
        result = decide(identity(Platform.WHATSAPP, window_open=False), "automation", templated)
        assert isinstance(result, Allowed)
        assert result.template_ref == "tpl-1"

    def test_whatsapp_has_no_human_agent_escape(self) -> None:
        """human_agent_days is None there, so an agent takes the template path
        like anybody else — and the engine learns that from the policy field,
        not from the platform's name."""
        result = decide(identity(Platform.WHATSAPP, window_open=False, last_inbound_days=1), "agent")
        assert isinstance(result, NeedsTemplate)


class TestBroadcastEligibility:
    def test_instagram_never_appears_in_a_broadcast(self) -> None:
        """SPEC §13.2, and it is refused before the window is even consulted."""
        result = decide(identity(Platform.INSTAGRAM, window_open=True), "broadcast")
        assert isinstance(result, Blocked)
        assert result.code == Denial.BROADCAST_NOT_ALLOWED

    @pytest.mark.parametrize("platform", [p for p in ALL_PLATFORMS if channel_policy.POLICIES[p].broadcast_allowed])
    def test_a_broadcast_platform_is_gated_only_by_the_ordinary_rules(self, platform: str) -> None:
        assert isinstance(decide(identity(platform, window_open=True), "broadcast"), Allowed)


class TestTheHumanAgentAllowanceCannotBeSelfGranted:
    """SPEC §22: "available only to inbox sends, never automation, hard-coded"."""

    @pytest.mark.parametrize("source", AUTOMATION_SOURCES)
    def test_a_non_agent_source_cannot_ask_for_the_tag(self, source: str) -> None:
        armed = OutboundMessage(tag=HUMAN_AGENT_TAG)
        result = decide(identity(Platform.INSTAGRAM, window_open=False, last_inbound_days=1), source, armed)
        assert not isinstance(result, Allowed)

    def test_an_allowed_decision_replaces_the_callers_tag(self) -> None:
        """The tag on the wire is the engine's answer, never the caller's ask —
        which is what stops an automation node self-granting the allowance on a
        platform where the window happens to be open."""
        armed = OutboundMessage(blocks=(TextBlock(text="hi"),), tag=HUMAN_AGENT_TAG)
        result = decide(identity(Platform.INSTAGRAM, window_open=True), "automation", armed)
        assert isinstance(result, Allowed)
        assert result.apply(armed).tag is None


class TestNoPerPlatformBranches:
    def test_the_module_names_no_platform(self) -> None:
        """Contract 4: Layer-5 adapters add a policy row and never patch this
        engine. A platform name appearing here is that promise breaking."""
        import inspect

        from apps.messaging import compliance

        source = inspect.getsource(compliance)
        body = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
        # Strip the docstrings, which legitimately name platforms in prose.
        for platform in Platform.values:
            assert f'"{platform}"' not in body
            assert f"Platform.{platform.upper()}" not in body

    def test_a_platform_with_no_policy_row_raises_rather_than_guessing(self) -> None:
        """policy_for() refuses to default, because the permissive answer is the
        wrong direction to guess in."""
        with pytest.raises(KeyError):
            decide(identity("carrier_pigeon", window_open=None), "automation")
