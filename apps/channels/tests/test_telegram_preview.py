"""SPEC §16's "test on Telegram": running a draft against a real chat.

Three claims from the issue's acceptance criteria are pinned here:

* the preview runs the **draft**, not the published version;
* it is **isolated per tester**;
* an expired or tampered link produces nothing an outsider can tell apart from
  a ``/start`` payload that was never a preview link at all.

The third is what stands in for SECURITY-BASELINE §4's "generic 404". There is
no HTTP route to 404: the handle travels through Telegram, and the webhook that
carries it back always answers 200. So the property is stated the only way it
can be — no preview, no reply, no distinguishable behaviour — and asserted
directly.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.channels import ingest as channels_ingest
from apps.channels import preview
from apps.channels.events import EventPayload, EventType, NormalizedEvent
from apps.channels.models import ChannelConnection, ConnectionStatus, FlowPreviewLink
from apps.channels.tests.telegram_support import BOT_TOKEN, fake_bot_api
from apps.common.encryption import hmac_digest
from apps.common.platforms import Platform
from apps.flows.models import FlowExecution
from apps.flows.services import create_flow, publish, save_draft
from apps.flows.tests.support import graph, node
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

CHAT = "5150"


def one_message_graph(text: str) -> dict[str, Any]:
    return graph([node("n1", "send_message", {"blocks": [{"type": "text", "text": text}]})], [])


@pytest.fixture
def telegram_connection(tenancy: Tenancy) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.TELEGRAM,
        display_name="@acme_bot",
        external_id="777000",
        credentials={"bot_token": BOT_TOKEN},
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


@pytest.fixture(autouse=True)
def pipeline() -> Iterator[None]:
    """Persistence, then preview — the composition production runs.

    ``apps/channels/tests/conftest.py`` clears the contract-6 seam for every
    test in this app, so a test that wants the real thing has to say so. Saying
    so is the point here: the preview stage **depends** on persistence having
    already run on the same ``/start``, because that is what records the
    tester's consent, and without it every first send is refused with
    ``no_opt_in``. Calling ``preview_events`` directly would hide exactly the
    coupling these tests exist to prove.
    """
    from apps.messaging.ingest import PERSISTENCE_PROCESSOR, persist_events

    channels_ingest.register_processor(persist_events, name=PERSISTENCE_PROCESSOR)
    channels_ingest.register_processor(preview.preview_events, name=preview.PREVIEW_PROCESSOR)
    yield


@pytest.fixture
def drafted_flow(tenancy: Tenancy) -> Any:
    """A flow whose published version says one thing and whose draft says another."""
    flow = create_flow(workspace=tenancy.workspace, name="Welcome")
    save_draft(flow, one_message_graph("PUBLISHED"))
    publish(flow)
    save_draft(flow, one_message_graph("DRAFT"))
    flow.refresh_from_db()
    return flow


def referral(connection: ChannelConnection, ref: str, chat_id: str = CHAT) -> NormalizedEvent:
    return NormalizedEvent(
        type=EventType.REFERRAL,
        connection=connection,
        platform_user_id=chat_id,
        provider_event_id=f"tg:{ref}:{chat_id}",
        timestamp=timezone.now(),
        payload=EventPayload(ref=ref),
    )


def deliver(connection: ChannelConnection, ref: str, chat_id: str = CHAT) -> None:
    """Put one event through the real dispatch, not straight into the stage."""
    channels_ingest.process_events(connection, (referral(connection, ref, chat_id),))


class TestPreviewRunsTheDraft:
    def test_the_draft_runs_not_the_published_version(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)

        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle))

        execution = FlowExecution.objects.unscoped().get(flow=drafted_flow)
        assert execution.flow_version.published is False
        # The runner sets this from `not version.published`, which is what keeps
        # a few test runs out of L7-A's counters.
        assert execution.preview is True
        assert execution.started_by.startswith("preview:")
        assert fake.payloads("sendMessage")[0]["text"] == "DRAFT"

    def test_the_tester_becomes_a_contact_on_that_chat(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        with fake_bot_api():
            deliver(telegram_connection, preview.start_payload(handle))

        execution = FlowExecution.objects.unscoped().get(flow=drafted_flow)
        assert execution.channel_connection_id == telegram_connection.pk
        assert execution.contact.workspace_id == tenancy.workspace.pk

    def test_a_draft_that_cannot_start_is_an_ordinary_outcome(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection
    ) -> None:
        """A draft is allowed to be half-wired — that is what a draft is."""
        flow = create_flow(workspace=tenancy.workspace, name="Half done")
        # Two entry candidates, so there is no single place to start.
        save_draft(flow, graph([node("a", "note", {"text": "x"}), node("b", "note", {"text": "y"}, x=200)], []))
        flow.refresh_from_db()
        _link, handle = preview.mint(flow=flow, connection=telegram_connection, user=tenancy.owner)

        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle))

        assert not FlowExecution.objects.unscoped().filter(flow=flow).exists()
        assert fake.calls == []

    def test_the_same_tester_may_restart_the_draft(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """Tapping the link again is the normal way this is used."""
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle))
            deliver(telegram_connection, preview.start_payload(handle))
        assert len(fake.payloads("sendMessage")) == 2


class TestIsolationPerTester:
    def test_a_second_chat_cannot_use_a_claimed_link(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """One editor's preview must not land in somebody else's conversation."""
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle), chat_id="1111")
            deliver(telegram_connection, preview.start_payload(handle), chat_id="2222")

        assert len(fake.payloads("sendMessage")) == 1
        assert fake.payloads("sendMessage")[0]["chat_id"] == "1111"

    def test_two_testers_with_their_own_links_do_not_collide(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        _first, first_handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        _second, second_handle = preview.mint(
            flow=drafted_flow, connection=telegram_connection, user=tenancy.user_for(WorkspaceRole.EDITOR)
        )
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(first_handle), chat_id="1111")
            deliver(telegram_connection, preview.start_payload(second_handle), chat_id="2222")

        assert [payload["chat_id"] for payload in fake.payloads("sendMessage")] == ["1111", "2222"]


class TestRejectedLinks:
    """Every one of these must be indistinguishable from an unrecognised ref."""

    def _assert_nothing_happened(self, fake: Any, flow: Any) -> None:
        assert fake.calls == []
        assert not FlowExecution.objects.unscoped().filter(flow=flow).exists()

    def test_an_expired_link_does_nothing(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        link.expires_at = timezone.now() - timedelta(seconds=1)
        link.save(update_fields=["expires_at", "updated_at"])

        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle))
        self._assert_nothing_happened(fake, drafted_flow)

    def test_a_tampered_handle_does_nothing(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        # One character changed. The lookup is an equality match on an HMAC, so
        # this is not a near miss — it is a different key entirely.
        tampered = ("A" if handle[0] != "A" else "B") + handle[1:]
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(tampered))
        self._assert_nothing_happened(fake, drafted_flow)

    def test_an_unknown_handle_does_nothing(self, telegram_connection: ChannelConnection, drafted_flow: Any) -> None:
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload("never-minted-anywhere"))
        self._assert_nothing_happened(fake, drafted_flow)

    def test_a_disabled_connection_does_nothing(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        telegram_connection.status = ConnectionStatus.DISABLED
        telegram_connection.save(update_fields=["status", "updated_at"])
        with fake_bot_api() as fake:
            deliver(telegram_connection, preview.start_payload(handle))
        self._assert_nothing_happened(fake, drafted_flow)

    def test_an_ordinary_ref_is_left_for_the_trigger_system(
        self, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """An operator's own ref-URL trigger must be untouched by this stage."""
        with fake_bot_api() as fake:
            deliver(telegram_connection, "spring-sale")
        self._assert_nothing_happened(fake, drafted_flow)

    def test_a_non_referral_event_is_ignored(self, telegram_connection: ChannelConnection, drafted_flow: Any) -> None:
        event = NormalizedEvent(
            type=EventType.MESSAGE,
            connection=telegram_connection,
            platform_user_id=CHAT,
            provider_event_id="tg:1",
            timestamp=timezone.now(),
            payload=EventPayload(text="preview-anything"),
        )
        with fake_bot_api() as fake:
            channels_ingest.process_events(telegram_connection, (event,))
        self._assert_nothing_happened(fake, drafted_flow)


class TestTheHandleIsNotStoredReadably:
    def test_only_the_digest_is_kept(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """A database snapshot must not hand over working links."""
        link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        link.refresh_from_db()
        assert link.handle_digest == hmac_digest(handle)
        assert handle not in str(link.__dict__)

    def test_the_handle_fits_telegrams_deep_link_budget(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """The reason this is a handle and not a signed token: a `start` payload
        is capped at 64 characters and restricted to [A-Za-z0-9_-]."""
        _link, handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        payload = preview.start_payload(handle)
        assert len(payload) <= 64
        assert all(character.isalnum() or character in "_-" for character in payload)


class TestMintEndpoint:
    def url(self, tenancy: Tenancy, flow: Any) -> str:
        return reverse(
            "channels:telegram_preview",
            kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk},
        )

    def test_it_returns_a_deep_link(
        self, client_for: Any, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        payload = client.post(self.url(tenancy, drafted_flow)).json()

        assert payload["ok"] is True
        assert payload["bot"] == "@acme_bot"
        assert payload["deep_link"].startswith("https://t.me/acme_bot?start=preview-")
        assert payload["expires_in"] == 900
        assert FlowPreviewLink.objects.for_workspace(tenancy.workspace).count() == 1

    def test_no_connection_is_an_explained_empty_state_not_an_error(
        self, client_for: Any, tenancy: Tenancy, drafted_flow: Any
    ) -> None:
        """A 4xx would send the builder down its API-error path and show a
        failure instead of "connect a bot first"."""
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        response = client.post(self.url(tenancy, drafted_flow))

        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is False
        assert payload["reason"] == "no_connection"
        assert payload["settings_url"].endswith("/telegram/connect/")

    def test_a_disabled_connection_does_not_count(
        self, client_for: Any, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        telegram_connection.status = ConnectionStatus.DISABLED
        telegram_connection.save(update_fields=["status", "updated_at"])
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        assert client.post(self.url(tenancy, drafted_flow)).json()["ok"] is False

    def test_a_viewer_may_not_mint_one(
        self, client_for: Any, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        """Testing sends real messages, so it takes edit_flows."""
        client = client_for(tenancy.user_for(WorkspaceRole.VIEWER))
        assert client.post(self.url(tenancy, drafted_flow)).status_code == 403

    def test_another_tenants_flow_is_404_not_403(
        self, client_for: Any, tenancy: Tenancy, other_tenancy: Tenancy, telegram_connection: ChannelConnection
    ) -> None:
        victim_flow = create_flow(workspace=other_tenancy.workspace, name="Theirs")
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        assert client.post(self.url(tenancy, victim_flow)).status_code == 404


class TestHousekeeping:
    def test_expired_links_are_pruned_after_a_grace_period(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        stale, _ = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        stale.expires_at = timezone.now() - timedelta(days=1)
        stale.save(update_fields=["expires_at", "updated_at"])
        fresh, _handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)

        assert preview.prune_expired_links() == 1
        assert list(FlowPreviewLink.objects.for_workspace(tenancy.workspace)) == [fresh]

    def test_a_link_that_only_just_lapsed_is_kept_for_debugging(
        self, tenancy: Tenancy, telegram_connection: ChannelConnection, drafted_flow: Any
    ) -> None:
        link, _handle = preview.mint(flow=drafted_flow, connection=telegram_connection, user=tenancy.owner)
        link.expires_at = timezone.now() - timedelta(seconds=30)
        link.save(update_fields=["expires_at", "updated_at"])
        assert preview.prune_expired_links() == 0


class TestConnectionsMadeByHand:
    """The generic "Add a channel" form still exists and asks for a display name.

    So a Telegram connection can carry any name at all, and a deep link built
    from one would be broken in a way the tester cannot diagnose. The mint
    endpoint checks the shape instead of trusting it.
    """

    def url(self, tenancy: Tenancy, flow: Any) -> str:
        return reverse(
            "channels:telegram_preview",
            kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk},
        )

    @pytest.mark.parametrize("display_name", ["My bot", "", "@ab", "@a" * 40, "@bot with spaces"])
    def test_a_display_name_that_is_not_a_username_is_an_empty_state(
        self, client_for: Any, tenancy: Tenancy, drafted_flow: Any, display_name: str
    ) -> None:
        ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name=display_name,
            external_id=f"hand-{len(display_name)}",
        )
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        payload = client.post(self.url(tenancy, drafted_flow)).json()
        assert payload["ok"] is False
        assert payload["reason"] == "no_username"
