"""Instagram as **data**: the policy row, the capability row, and the proof.

ROADMAP contract 4's promise is that a platform costs "one module in
``channels/providers/`` and one registry entry", and that "the compliance engine
consumes policy entries as data — Layer-5 adapters never patch it".

The compliance *verdicts* are asserted where the engine lives
(``apps/messaging/tests/test_compliance.py`` covers SPEC §21's three Instagram
cases, and ``test_compliance_setwise.py`` parametrises over every platform in the
policy table). What is asserted here is the other half: that the rows say what
SPEC §6.3 says, and that nothing in ``apps.messaging`` had to learn the word
"Instagram" to make them work.
"""

import ast
from pathlib import Path

import pytest

from apps.channels.capabilities import capabilities_for
from apps.channels.policy import policy_for
from apps.channels.registry import CONNECT_ROUTES, adapter_for, entry_for
from apps.common.platforms import Platform

MESSAGING = Path(__file__).resolve().parents[3] / "messaging"


class TestThePolicyRow:
    def test_it_says_what_spec_6_3_says(self) -> None:
        policy = policy_for(Platform.INSTAGRAM)
        # "Window: 24h from last inbound user message."
        assert policy.window_hours == 24
        # "automation -> Blocked" outside it (SPEC §8).
        assert policy.outside_window == "blocked"
        # "HUMAN_AGENT tag extends to 7 days, allowed only for agent sends."
        assert policy.human_agent_days == 7
        # "broadcast_allowed false", and SPEC §13.2: "Instagram never appears in
        # the broadcast channel selector."
        assert policy.broadcast_allowed is False
        # SPEC §8's default token-bucket rate.
        assert policy.rate_default == 8.0

    def test_the_capability_row_agrees_with_it(self) -> None:
        """``window_hours`` and ``broadcast_allowed`` are stored twice — the
        policy is authoritative and SPEC §6.1 puts both in the capability list
        the flow builder shows."""
        policy = policy_for(Platform.INSTAGRAM)
        capabilities = capabilities_for(Platform.INSTAGRAM)
        assert capabilities.window_hours == policy.window_hours
        assert capabilities.broadcast_allowed == policy.broadcast_allowed

    def test_proactive_send_is_off(self) -> None:
        """Outside the window automation is Blocked, and HUMAN_AGENT is an
        agent-only path the compliance engine owns rather than a capability."""
        assert capabilities_for(Platform.INSTAGRAM).proactive_send is False

    def test_the_only_tag_is_human_agent(self) -> None:
        assert capabilities_for(Platform.INSTAGRAM).tags_supported == ("HUMAN_AGENT",)

    def test_the_limits_are_metas(self) -> None:
        capabilities = capabilities_for(Platform.INSTAGRAM)
        # "Message text must be UTF-8 and be a 1000 bytes or less."
        assert capabilities.max_text_len == 1000
        # Generic template: three buttons per element, thirteen quick replies.
        assert capabilities.max_buttons == 3
        assert capabilities.max_quick_replies == 13
        # No generic document attachment on Instagram messaging.
        assert capabilities.file is False


class TestTheRegistryEntry:
    def test_the_adapter_is_registered(self) -> None:
        from apps.channels.providers.instagram import InstagramAdapter

        assert isinstance(adapter_for(Platform.INSTAGRAM), InstagramAdapter)

    def test_the_entry_carries_all_three(self) -> None:
        entry = entry_for(Platform.INSTAGRAM)
        assert entry.adapter_cls is not None
        assert entry.policy is policy_for(Platform.INSTAGRAM)
        assert entry.capabilities is capabilities_for(Platform.INSTAGRAM)

    def test_the_adapter_reads_the_shared_capability_singleton(self) -> None:
        """Layer-5 adapters *read* the table; a mutable patch would reconfigure
        the whole deployment (``apps.channels.capabilities``)."""
        assert adapter_for(Platform.INSTAGRAM).capabilities is capabilities_for(Platform.INSTAGRAM)

    def test_a_guided_connect_route_is_declared(self) -> None:
        """Which is also what makes the generic "Add a channel" form refuse
        Instagram: that form creates a row with no credentials, and every send
        on one fails."""
        assert CONNECT_ROUTES[Platform.INSTAGRAM.value] == "channels:instagram_connect"

    def test_the_generic_form_refuses_it(self) -> None:
        """The narrowing is on the widget and the ``clean_platform`` check, not
        on the field's choices — see ``apps.channels.forms``, which explains why
        Django's own choice validation is the wrong place for it."""
        from apps.channels.forms import ChannelConnectionForm

        form = ChannelConnectionForm()
        offered = [value for value, _label in form.fields["platform"].widget.choices if value]
        assert Platform.INSTAGRAM.value not in offered

        submitted = ChannelConnectionForm(
            data={"platform": Platform.INSTAGRAM.value, "display_name": "@x", "external_id": "1"}
        )
        assert not submitted.is_valid()
        assert "platform" in submitted.errors


class TestSharedExtraKeys:
    """The ``payload.extra`` keys that cross the contract-6 seam, pinned.

    ``apps.channels`` sits below ``apps.messaging``, so these keys are literals
    on both sides and are documented in the module that reads them. That is the
    same shape ``apps.flows.triggers.pipeline`` uses for ``ROUTING_PROCESSOR``,
    and it comes with the same obligation: pin the duplication, or it drifts in
    silence.
    """

    def test_the_private_reply_marker_agrees_across_apps(self) -> None:
        """Rename one side and every comment-to-DM stops opening a messaging
        window, so every private reply is Blocked by the compliance engine —
        with nothing raising anywhere."""
        from apps.channels.providers import instagram
        from apps.messaging import ingest

        assert instagram.PRIVATE_REPLY_CLAIMED_KEY == ingest.PRIVATE_REPLY_CLAIMED_KEY

    def test_the_provider_message_id_key_agrees_across_apps(self) -> None:
        """Rename one side and a ``message_deletions`` delivery stops finding the
        row it is meant to redact (SPEC §6.3, §19)."""
        from apps.channels.providers import instagram
        from apps.messaging import ingest

        assert instagram.PROVIDER_MESSAGE_ID_KEY == ingest.PROVIDER_MESSAGE_ID_KEY


class TestContractFour:
    """ "No platform branch anywhere in ``apps/messaging/``" — asserted, not promised."""

    def _sources(self) -> list[Path]:
        return [
            path
            for path in MESSAGING.rglob("*.py")
            # Migrations declare the platform choices; tests name platforms
            # deliberately, which is where the engine's own behaviour is pinned.
            if "migrations" not in path.parts and "tests" not in path.parts
        ]

    def test_no_instagram_literal_reaches_messaging_code(self) -> None:
        """A docstring may name it — SPEC §6.3 is *why* deletion redaction exists
        — but no expression may. That is the same standard Telegram is held to:
        ``grep -rn telegram apps/messaging/`` finds a migration's choices list and
        a docstring, and nothing else.
        """
        offenders: list[str] = []
        for path in self._sources():
            tree = ast.parse(path.read_text())
            docstrings = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings or "instagram" not in node.value.lower():
                    continue
                offenders.append(f"{path.relative_to(MESSAGING.parent)}:{node.lineno}")
        assert offenders == [], (
            f"apps/messaging must branch on PlatformPolicy data, never on a platform name. "
            f"Found Instagram in: {offenders}"
        )

    @pytest.mark.parametrize("platform", sorted(Platform.values))
    def test_no_platform_at_all_reaches_messaging_code(self, platform: str) -> None:
        """The general form. Contract 4 is additive for every platform or for none."""
        offenders: list[str] = []
        for path in self._sources():
            tree = ast.parse(path.read_text())
            docstrings = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstrings or node.value.lower() != platform:
                    continue
                offenders.append(f"{path.relative_to(MESSAGING.parent)}:{node.lineno}")
        assert offenders == []
