"""The endpoint's half of Instagram: signatures, verification, and tenancy.

``parse_events`` is tested next door. This is about everything the *endpoint*
does around it — the raw-body HMAC with the app secret rather than a per-connection
one, Meta's ``hub.challenge`` GET, and what happens to a batch that names more
than one account.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.test import Client

from apps.channels import security
from apps.channels.models import ChannelConnection, ConnectionStatus, WebhookEventLog
from apps.channels.providers.meta_common import SIGNATURE_HEADER
from apps.channels.tests.instagram_support import APP_SECRET, at_now, load_delivery, sign
from apps.common.platforms import Platform
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

WEBHOOK_URL = "/webhooks/instagram/"


@pytest.fixture
def collected() -> Iterator[list[Any]]:
    """Every event the seam receives, so the endpoint can be observed end to end."""
    from apps.channels import ingest as channels_ingest

    events: list[Any] = []
    channels_ingest.register_processor(lambda _c, batch: events.extend(batch), name="collector")
    yield events


def deliver(client: Client, payload: Any, *, signature: str | None = None) -> Any:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {} if signature is None else {SIGNATURE_HEADER: signature}
    return client.post(WEBHOOK_URL, data=body, content_type="application/json", headers=headers)


@pytest.mark.usefixtures("instagram_app")
class TestSignature:
    def test_a_correctly_signed_delivery_is_accepted(
        self, client: Client, instagram_connection: ChannelConnection, collected: list[Any]
    ) -> None:
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        response = client.post(
            WEBHOOK_URL, data=body, content_type="application/json", headers={SIGNATURE_HEADER: sign(body)}
        )
        assert response.status_code == 200
        assert len(collected) == 1

    def test_no_signature_is_403(self, client: Client, instagram_connection: ChannelConnection) -> None:
        assert deliver(client, at_now(load_delivery("message_text"))).status_code == 403

    def test_a_wrong_signature_is_403(self, client: Client, instagram_connection: ChannelConnection) -> None:
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body, "not-the-app-secret")).status_code == 403

    def test_a_signature_over_a_different_body_is_403(
        self, client: Client, instagram_connection: ChannelConnection
    ) -> None:
        """The HMAC is over the **raw** body, before parsing — so re-serialising
        or tampering after signing cannot match."""
        payload = at_now(load_delivery("message_text"))
        signature = sign(json.dumps(payload).encode())
        payload["entry"][0]["messaging"][0]["message"]["text"] = "swapped"
        assert deliver(client, payload, signature=signature).status_code == 403

    def test_a_malformed_signature_header_is_403_not_a_different_answer(
        self, client: Client, instagram_connection: ChannelConnection
    ) -> None:
        """A distinguishable "malformed header" reply is a free oracle telling an
        attacker their format is right."""
        payload = at_now(load_delivery("message_text"))
        for value in ("", "sha256=", "nonsense", "sha1=abc", "sha256=zz"):
            assert deliver(client, payload, signature=value).status_code == 403

    def test_with_no_app_credentials_every_delivery_is_refused(
        self, client: Client, instagram_connection: ChannelConnection, settings: Any
    ) -> None:
        """Fails closed, and indistinguishably from a wrong signature."""
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 403

    def test_a_workspace_override_secret_is_used(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection, collected: list[Any]
    ) -> None:
        """SPEC §4's chain: the most specific *usable* level wins, and rotating
        an app secret in the admin has to take effect on the next delivery."""
        from apps.credentials.models import WorkspaceCredentialOverride

        override = WorkspaceCredentialOverride(workspace=tenancy.workspace, platform=Platform.INSTAGRAM.value)
        # EncryptedJSONField subclasses TextField, so django-stubs types the
        # attribute as str even though the column holds json — the same
        # suppression ``instagram_oauth.store_credentials`` carries.
        override.credentials = {"client_id": "override", "client_secret": "workspace-level-secret"}  # type: ignore[assignment]
        override.save()
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()

        assert deliver(client, body, signature=sign(body, APP_SECRET)).status_code == 403
        assert deliver(client, body, signature=sign(body, "workspace-level-secret")).status_code == 200


@pytest.mark.usefixtures("instagram_app")
class TestConnectionState:
    def test_a_disabled_connection_stops_ingesting(
        self, client: Client, instagram_connection: ChannelConnection
    ) -> None:
        instagram_connection.status = ConnectionStatus.DISABLED
        instagram_connection.save(update_fields=["status", "updated_at"])
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 403

    def test_a_connection_needing_reauth_still_receives(
        self, client: Client, instagram_connection: ChannelConnection, collected: list[Any]
    ) -> None:
        """Inbound is what tells an operator the channel is alive, and a stale
        *outbound* token says nothing about Meta's ability to deliver to us."""
        instagram_connection.status = ConnectionStatus.NEEDS_REAUTH
        instagram_connection.save(update_fields=["status", "updated_at"])
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 200
        assert len(collected) == 1


@pytest.mark.usefixtures("instagram_app")
class TestBatchesAcrossAccounts:
    def test_two_accounts_in_one_workspace_are_both_logged(
        self, client: Client, tenancy: Tenancy, instagram_connection: ChannelConnection
    ) -> None:
        second = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.INSTAGRAM,
            display_name="@other",
            external_id="17841400000000002",
        )
        payload = at_now(load_delivery("message_text"))
        payload["entry"].append(json.loads(json.dumps(payload["entry"][0])))
        payload["entry"][1]["id"] = second.external_id
        payload["entry"][1]["messaging"][0]["message"]["mid"] = "aWdfZG1fOTox"

        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 200
        assert WebhookEventLog.objects.filter(connection=instagram_connection).count() == 1
        assert WebhookEventLog.objects.filter(connection=second).count() == 1

    def test_an_entry_for_another_tenants_account_is_dropped(
        self,
        client: Client,
        tenancy: Tenancy,
        other_tenancy: Tenancy,
        instagram_connection: ChannelConnection,
    ) -> None:
        """SECURITY-BASELINE §1. Where a workspace supplies its own Meta app
        credentials, the signature is *its* secret — so without this, a tenant
        could sign a delivery for its own account and staple on an entry naming
        somebody else's."""
        victim = ChannelConnection.objects.create(
            workspace=other_tenancy.workspace,
            platform=Platform.INSTAGRAM,
            display_name="@victim",
            external_id="17841400000000009",
        )
        payload = at_now(load_delivery("message_text"))
        payload["entry"].append(json.loads(json.dumps(payload["entry"][0])))
        payload["entry"][1]["id"] = victim.external_id
        payload["entry"][1]["messaging"][0]["message"]["mid"] = "aWdfZG1fOTox"

        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 200
        assert WebhookEventLog.objects.filter(connection=instagram_connection).count() == 1
        assert not WebhookEventLog.objects.filter(connection=victim).exists()


@pytest.mark.usefixtures("instagram_app")
class TestDeduplication:
    def test_a_redelivered_event_is_logged_once(
        self, client: Client, instagram_connection: ChannelConnection, collected: list[Any]
    ) -> None:
        payload = at_now(load_delivery("message_text"))
        body = json.dumps(payload).encode()
        deliver(client, body, signature=sign(body))
        deliver(client, body, signature=sign(body))
        assert WebhookEventLog.objects.filter(connection=instagram_connection).count() == 1
        assert len(collected) == 1


class TestHubChallenge:
    def test_meta_verification_echoes_the_challenge(self, client: Client, instagram_app: Any) -> None:
        response = client.get(
            WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": "hub-verify-token", "hub.challenge": "123456"},
        )
        assert response.status_code == 200
        assert response.content == b"123456"

    def test_a_wrong_verify_token_is_403(self, client: Client, instagram_app: Any) -> None:
        response = client.get(
            WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "123456"},
        )
        assert response.status_code == 403

    def test_without_a_configured_token_the_endpoint_does_not_advertise_itself(
        self, client: Client, settings: Any
    ) -> None:
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
        response = client.get(
            WEBHOOK_URL,
            {"hub.mode": "subscribe", "hub.verify_token": "anything", "hub.challenge": "1"},
        )
        assert response.status_code == 404


class TestBodyLimits:
    def test_an_oversized_body_is_refused_before_it_is_read(
        self, client: Client, instagram_connection: ChannelConnection, instagram_app: Any
    ) -> None:
        payload = at_now(load_delivery("message_text"))
        payload["entry"][0]["messaging"][0]["message"]["text"] = "x" * (security.max_body_bytes() + 10)
        body = json.dumps(payload).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 413

    def test_a_nesting_bomb_never_reaches_the_parser(
        self, client: Client, instagram_connection: ChannelConnection, instagram_app: Any
    ) -> None:
        depth = security.json_depth_limit() + 20
        body = ("[" * depth + "]" * depth).encode()
        assert deliver(client, body, signature=sign(body)).status_code == 400
