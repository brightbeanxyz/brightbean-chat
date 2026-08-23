"""ROADMAP contract 4: the registry, the policy table and the capability table.

The point of these tests is that the contract is *fixed*. A Layer-5 adapter
author who renames a field or drops a platform breaks this file rather than
breaking L2-D's builder warnings or L3-A's compliance engine, neither of which
would notice until something got sent — or failed to be.
"""

import dataclasses

import pytest

from apps.channels.capabilities import CAPABILITIES, Capabilities, capabilities_for
from apps.channels.policy import POLICIES, NeedsTag, PlatformPolicy, policy_for
from apps.channels.providers.base import Adapter
from apps.channels.registry import (
    AdapterNotRegisteredError,
    adapter_for,
    entry_for,
    has_adapter,
    register_adapter,
    registered_platforms,
    unregister_adapter,
)
from apps.channels.tests.fake_adapter import fake_adapter_for, registered
from apps.common.platforms import Platform


class TestTablesCoverEveryPlatform:
    def test_every_platform_has_capabilities(self) -> None:
        assert set(CAPABILITIES) == set(Platform.values)

    def test_every_platform_has_a_policy(self) -> None:
        assert set(POLICIES) == set(Platform.values)

    def test_unknown_platform_raises_rather_than_defaulting(self) -> None:
        # Defaulting would answer "no window, no tags, broadcast away", which is
        # the most permissive reading available.
        with pytest.raises(KeyError):
            capabilities_for("carrier-pigeon")
        with pytest.raises(KeyError):
            policy_for("carrier-pigeon")


class TestContractFieldsAreExactlyAsWritten:
    """ROADMAP contract 4 spells the dataclass out; this asserts the spelling."""

    def test_platform_policy_fields(self) -> None:
        fields = {f.name for f in dataclasses.fields(PlatformPolicy)}
        assert fields == {
            "window_hours",
            "outside_window",
            "human_agent_days",
            "broadcast_allowed",
            "rate_default",
        }

    def test_needs_tag_fields(self) -> None:
        assert {f.name for f in dataclasses.fields(NeedsTag)} == {"tags", "allowed_use_text"}

    def test_capabilities_carries_every_spec_6_1_flag(self) -> None:
        fields = {f.name for f in dataclasses.fields(Capabilities)}
        assert fields == {
            "text",
            "image",
            "audio",
            "video",
            "file",
            "card",
            "gallery",
            "buttons",
            "quick_replies",
            "url_buttons",
            "typing_indicator",
            "proactive_send",
            "window_hours",
            "tags_supported",
            "max_buttons",
            "max_quick_replies",
            "max_text_len",
            "broadcast_allowed",
            "inbound",
        }

    def test_tables_are_frozen(self) -> None:
        # Module-level singletons shared by every request in the worker.
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy_for(Platform.TELEGRAM).rate_default = 1.0  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            capabilities_for(Platform.TELEGRAM).text = False  # type: ignore[misc]


class TestTheTwoTablesAgree:
    """window_hours and broadcast_allowed appear in both. They must not drift."""

    @pytest.mark.parametrize("platform", Platform.values)
    def test_window_hours_matches(self, platform: str) -> None:
        assert capabilities_for(platform).window_hours == policy_for(platform).window_hours

    @pytest.mark.parametrize("platform", Platform.values)
    def test_broadcast_allowed_matches(self, platform: str) -> None:
        assert capabilities_for(platform).broadcast_allowed == policy_for(platform).broadcast_allowed


class TestSpecValues:
    """The handful of values SPEC §§6 and 8 state outright."""

    def test_instagram_blocks_automation_outside_the_window(self) -> None:
        policy = policy_for(Platform.INSTAGRAM)
        assert policy.window_hours == 24
        assert policy.outside_window == "blocked"
        assert policy.human_agent_days == 7
        assert policy.broadcast_allowed is False

    def test_messenger_needs_a_non_promotional_tag(self) -> None:
        policy = policy_for(Platform.MESSENGER)
        assert isinstance(policy.outside_window, NeedsTag)
        assert set(policy.outside_window.tags) == {
            "CONFIRMED_EVENT_UPDATE",
            "POST_PURCHASE_UPDATE",
            "ACCOUNT_UPDATE",
        }
        # SPEC §6.4 requires the composer to display Meta's allowed-use text.
        assert policy.outside_window.allowed_use_text

    def test_whatsapp_needs_a_template(self) -> None:
        assert policy_for(Platform.WHATSAPP).outside_window == "needs_template"

    def test_windowless_platforms_say_so(self) -> None:
        for platform in (Platform.TELEGRAM, Platform.SMS, Platform.EMAIL):
            assert policy_for(platform).has_window() is False

    def test_rate_defaults_match_spec_8(self) -> None:
        assert {p: policy_for(p).rate_default for p in Platform.values} == {
            Platform.TELEGRAM: 25.0,
            Platform.INSTAGRAM: 8.0,
            Platform.MESSENGER: 40.0,
            Platform.WHATSAPP: 20.0,
            Platform.SMS: 1.0,
            Platform.EMAIL: 10.0,
        }

    def test_email_is_outbound_only(self) -> None:
        # SPEC §6.7. The /webhooks/email/ route carries bounce notifications,
        # not inbound messages.
        assert capabilities_for(Platform.EMAIL).inbound is False


class TestSupportsBlock:
    def test_known_kinds_read_their_flag(self) -> None:
        telegram = capabilities_for(Platform.TELEGRAM)
        assert telegram.supports_block("image") is True
        assert telegram.supports_block("gallery") is False

    def test_unknown_kinds_are_unsupported(self) -> None:
        assert capabilities_for(Platform.TELEGRAM).supports_block("hologram") is False

    def test_non_block_attributes_cannot_be_read_as_blocks(self) -> None:
        # `inbound` and `broadcast_allowed` are True for Telegram; asking for
        # them as a block kind must still answer False.
        telegram = capabilities_for(Platform.TELEGRAM)
        assert telegram.supports_block("inbound") is False
        assert telegram.supports_block("broadcast_allowed") is False


class TestRegistry:
    def test_entry_is_complete_without_an_adapter(self) -> None:
        """The property L2-D depends on: policy and capabilities today, adapters later."""
        entry = entry_for(Platform.WHATSAPP)
        assert entry.adapter_cls is None
        assert entry.policy is policy_for(Platform.WHATSAPP)
        assert entry.capabilities is capabilities_for(Platform.WHATSAPP)

    def test_register_and_resolve(self) -> None:
        with registered(Platform.TELEGRAM) as adapter_cls:
            assert has_adapter(Platform.TELEGRAM)
            assert registered_platforms() == (Platform.TELEGRAM,)
            assert isinstance(adapter_for(Platform.TELEGRAM), adapter_cls)
            assert isinstance(adapter_for(Platform.TELEGRAM), Adapter)
        # Restored, not cleared. Telegram has a real adapter since issue #12 and
        # `registered` puts it back; the property under test is that the fake
        # does not outlive the block, not that the slot ends up empty.
        assert has_adapter(Platform.TELEGRAM)
        assert not isinstance(adapter_for(Platform.TELEGRAM), adapter_cls)

    def test_missing_adapter_raises_rather_than_returning_none(self) -> None:
        # On the webhook path, None would read as "nothing to do" — a silently
        # dropped delivery.
        with pytest.raises(AdapterNotRegisteredError):
            adapter_for(Platform.WHATSAPP)

    def test_unknown_platform_cannot_be_registered(self) -> None:
        with pytest.raises(ValueError, match="Unknown platform"):
            register_adapter("carrier-pigeon", fake_adapter_for(Platform.TELEGRAM))

    def test_double_registration_is_refused(self) -> None:
        with registered(Platform.TELEGRAM), pytest.raises(ValueError, match="already has an adapter"):
            register_adapter(Platform.TELEGRAM, fake_adapter_for(Platform.TELEGRAM))

    def test_registering_the_same_class_twice_is_idempotent(self) -> None:
        adapter_cls = fake_adapter_for(Platform.SMS)
        register_adapter(Platform.SMS, adapter_cls)
        try:
            register_adapter(Platform.SMS, adapter_cls)
        finally:
            unregister_adapter(Platform.SMS)
