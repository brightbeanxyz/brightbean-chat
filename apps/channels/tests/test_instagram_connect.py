"""The OAuth round trip, and the ``state`` parameter that makes it safe.

The threat this file is mostly about: without a verified ``state``, anyone who
can make an operator's browser land on the callback with a code of *their*
choosing connects **their** Instagram account into the operator's workspace, and
every DM to that account then arrives in a stranger's inbox. So the assertions
below are less about the happy path than about the four ways the callback has to
refuse — tampered, expired, minted for another user, minted for a workspace the
signed-in user may not administer — and about refusing *before* anything is
exchanged with Meta or written to the database.
"""

import json
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from django.urls import reverse
from django.utils import timezone

from apps.channels import instagram_oauth as oauth
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.tests.instagram_support import ACCESS_TOKEN, IG_ACCOUNT_ID
from apps.common.platforms import Platform
from apps.notifications.models import Notification
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

CALLBACK = "/channels/instagram/callback/"

LONG_LIVED = "IGAAQZBlonglivedExampleExampleExampleExampleExampleExampleZD"  # noqa: S105 - a fake credential


@contextmanager
def fake_oauth_api(handler: Callable[[httpx.Request], httpx.Response] | None = None) -> Iterator[list[httpx.Request]]:
    """Answer every token exchange in memory, through the module's own seam."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler is not None:
            return handler(request)
        path = request.url.path
        if path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "short-lived", "user_id": int(IG_ACCOUNT_ID)})
        if path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": LONG_LIVED, "expires_in": 5_183_944})
        if path.endswith("/refresh_access_token"):
            return httpx.Response(200, json={"access_token": LONG_LIVED, "expires_in": 5_183_944})
        return httpx.Response(200, json={"user_id": IG_ACCOUNT_ID, "username": "brightbean"})

    client = httpx.Client(transport=httpx.MockTransport(respond))
    original = oauth._client
    oauth._client = lambda: client  # type: ignore[assignment]
    try:
        yield seen
    finally:
        oauth._client = original  # type: ignore[assignment]
        client.close()


def connect_url(tenancy: Tenancy) -> str:
    return reverse("channels:instagram_connect", kwargs={"workspace_id": tenancy.workspace.pk})


class TestState:
    def test_a_state_round_trips(self, tenancy: Tenancy) -> None:
        token = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        assert oauth.read_state(token) == {"ws": str(tenancy.workspace.pk), "u": str(tenancy.owner.pk)}

    @pytest.mark.parametrize("token", ["", "nonsense", "a.b.c", "x" * 500])
    def test_a_tampered_state_is_refused(self, token: str) -> None:
        assert oauth.read_state(token) is None

    def test_a_mutated_signature_is_refused(self, tenancy: Tenancy) -> None:
        token = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        assert oauth.read_state(token[:-1] + ("a" if token[-1] != "a" else "b")) is None

    def test_a_state_minted_for_another_purpose_is_refused(self, tenancy: Tenancy) -> None:
        """The purpose is the signer salt, so a token minted for the unsubscribe
        route cannot be replayed here even though both use SECRET_KEY."""
        from apps.common import signing

        token = signing.sign({"ws": str(tenancy.workspace.pk), "u": "1"}, purpose="unsubscribe")
        assert oauth.read_state(token) is None

    def test_an_expired_state_is_refused(self, tenancy: Tenancy, monkeypatch: Any) -> None:
        token = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        monkeypatch.setattr(oauth, "STATE_MAX_AGE", -1)
        assert oauth.read_state(token) is None


class TestConnectPage:
    def test_it_needs_manage_channels(self, tenancy: Tenancy, client_for: Any) -> None:
        response = client_for(tenancy.user_for("viewer")).get(connect_url(tenancy))
        assert response.status_code == 403

    def test_it_explains_the_callback_url(self, tenancy: Tenancy, client_for: Any, instagram_app: Any) -> None:
        response = client_for(tenancy.owner).get(connect_url(tenancy))
        assert response.status_code == 200
        assert oauth.callback_url().encode() in response.content

    def test_posting_redirects_to_meta_with_a_state(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        response = client_for(tenancy.owner).post(connect_url(tenancy))
        assert response.status_code == 302
        assert response["Location"].startswith(oauth.AUTHORIZE_URL)
        assert "instagram_business_manage_comments" in response["Location"]

    def test_without_app_credentials_it_says_so_rather_than_redirecting(
        self, tenancy: Tenancy, client_for: Any, settings: Any
    ) -> None:
        settings.PLATFORM_CREDENTIALS_FROM_ENV = {}
        response = client_for(tenancy.owner).post(connect_url(tenancy))
        assert response.status_code == 200
        assert b"no Instagram app credentials" in response.content


class TestCallback:
    def test_a_full_round_trip_creates_the_connection(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        with fake_oauth_api():
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 302

        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert connection.platform == Platform.INSTAGRAM
        # The professional-account id, because that is what arrives as entry[].id.
        assert connection.external_id == IG_ACCOUNT_ID
        assert connection.display_name == "@brightbean"
        assert oauth.access_token(connection) == LONG_LIVED
        assert oauth.token_expires_at(connection) is not None

    def test_a_tampered_state_is_refused_before_any_exchange(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        with fake_oauth_api() as seen:
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": "forged"})
        assert response.status_code == 404
        # Nothing was said to Meta and nothing was written.
        assert seen == []
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_a_state_for_another_user_is_refused(self, tenancy: Tenancy, client_for: Any, instagram_app: Any) -> None:
        """A shared machine, a stale tab, or an attempt to graft a connection
        onto somebody else's session."""
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.user_for("admin").pk)
        with fake_oauth_api() as seen:
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 404
        assert seen == []

    def test_a_state_for_another_tenants_workspace_is_refused(
        self, tenancy: Tenancy, other_tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        """The cross-tenant case, which the IDOR sweep cannot reach: this route
        carries no workspace kwarg, so there is nothing for it to substitute.

        The state is forged as if the attacker had somehow obtained one for the
        victim's workspace; the signed-in user is still theirs, and the
        membership lookup is what refuses.
        """
        state = oauth.sign_state(
            workspace_id=other_tenancy.workspace.pk,
            user_id=tenancy.owner.pk,
        )
        with fake_oauth_api() as seen:
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 404
        assert seen == []
        assert not ChannelConnection.objects.for_workspace(other_tenancy.workspace).exists()

    def test_a_member_without_manage_channels_is_refused(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        viewer = tenancy.user_for("viewer")
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=viewer.pk)
        with fake_oauth_api() as seen:
            response = client_for(viewer).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 404
        assert seen == []

    def test_an_anonymous_caller_never_reaches_the_exchange(self, client_for: Any, tenancy: Tenancy) -> None:
        from django.test import Client

        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        with fake_oauth_api() as seen:
            response = Client().get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]
        assert seen == []

    def test_a_cancelled_authorisation_is_not_an_error(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any
    ) -> None:
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        with fake_oauth_api() as seen:
            response = client_for(tenancy.owner).get(
                CALLBACK, {"error": "access_denied", "error_reason": "user_denied", "state": state}
            )
        assert response.status_code == 302
        assert seen == []

    def test_a_refused_exchange_leaves_no_trace(self, tenancy: Tenancy, client_for: Any, instagram_app: Any) -> None:
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)

        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "bad code", "code": 100}})

        with fake_oauth_api(refuse):
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 302
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_reconnecting_refreshes_the_existing_row(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any, instagram_connection: ChannelConnection
    ) -> None:
        """Deleting first would take the conversations, triggers and identities
        with it. Reconnecting is the ordinary way out of ``needs_reauth``."""
        instagram_connection.status = ConnectionStatus.NEEDS_REAUTH
        instagram_connection.save(update_fields=["status", "updated_at"])

        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        with fake_oauth_api():
            client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state})

        instagram_connection.refresh_from_db()
        assert instagram_connection.status == ConnectionStatus.ACTIVE
        assert oauth.access_token(instagram_connection) == LONG_LIVED
        assert ChannelConnection.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_account_another_workspace_holds_is_not_stolen(
        self,
        tenancy: Tenancy,
        other_tenancy: Tenancy,
        client_for: Any,
        instagram_app: Any,
        instagram_connection: ChannelConnection,
    ) -> None:
        """SPEC §5's unique (platform, external_id) is deployment-wide, and the
        reconnect path is scoped to the caller's workspace — so connecting an
        account somebody else holds fails rather than handing over their traffic.
        """
        attacker = other_tenancy.owner
        state = oauth.sign_state(workspace_id=other_tenancy.workspace.pk, user_id=attacker.pk)
        with fake_oauth_api():
            response = client_for(attacker).get(CALLBACK, {"code": "auth-code", "state": state})
        assert response.status_code == 302
        assert not ChannelConnection.objects.for_workspace(other_tenancy.workspace).exists()
        instagram_connection.refresh_from_db()
        assert instagram_connection.workspace_id == tenancy.workspace.pk

    def test_no_token_reaches_a_log_or_the_page(
        self, tenancy: Tenancy, client_for: Any, instagram_app: Any, caplog: Any
    ) -> None:
        state = oauth.sign_state(workspace_id=tenancy.workspace.pk, user_id=tenancy.owner.pk)
        with caplog.at_level(logging.DEBUG), fake_oauth_api():
            response = client_for(tenancy.owner).get(CALLBACK, {"code": "auth-code", "state": state}, follow=True)
        assert LONG_LIVED not in caplog.text
        assert LONG_LIVED.encode() not in response.content
        assert instagram_app["client_secret"] not in caplog.text


class TestTokenRefresh:
    def test_a_token_near_expiry_is_refreshed(self, instagram_connection: ChannelConnection) -> None:
        oauth.store_credentials(
            instagram_connection,
            token=ACCESS_TOKEN,
            expires_at=timezone.now() + timedelta(days=2),
            user_id=instagram_connection.external_id,
        )
        instagram_connection.save(update_fields=["credentials", "updated_at"])

        with fake_oauth_api():
            assert oauth.refresh_expiring_tokens() == 1

        instagram_connection.refresh_from_db()
        assert oauth.access_token(instagram_connection) == LONG_LIVED

    def test_a_healthy_token_is_left_alone(self, instagram_connection: ChannelConnection) -> None:
        with fake_oauth_api() as seen:
            assert oauth.refresh_expiring_tokens() == 0
        assert seen == []

    def test_a_refused_refresh_marks_the_channel_and_notifies(self, instagram_connection: ChannelConnection) -> None:
        """A silent failure here means the account stops working in sixty days
        and nobody finds out."""
        oauth.store_credentials(
            instagram_connection,
            token=ACCESS_TOKEN,
            expires_at=timezone.now() + timedelta(days=1),
            user_id=instagram_connection.external_id,
        )
        instagram_connection.save(update_fields=["credentials", "updated_at"])

        def refuse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": {"message": "expired", "code": 190}})

        with fake_oauth_api(refuse):
            assert oauth.refresh_expiring_tokens() == 0

        instagram_connection.refresh_from_db()
        assert instagram_connection.status == ConnectionStatus.NEEDS_REAUTH
        assert Notification.objects.filter(event_type="channel_needs_reauth").exists()

    def test_a_connection_with_no_token_is_reported_not_swept_past(
        self, instagram_connection: ChannelConnection
    ) -> None:
        instagram_connection.credentials = {}  # type: ignore[assignment]
        instagram_connection.save(update_fields=["credentials", "updated_at"])
        with fake_oauth_api() as seen:
            assert oauth.refresh_expiring_tokens() == 0
        assert seen == []
        instagram_connection.refresh_from_db()
        assert instagram_connection.status == ConnectionStatus.NEEDS_REAUTH

    def test_a_refresh_that_returns_nothing_usable_is_an_error(self, instagram_connection: ChannelConnection) -> None:
        def empty(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "bearer"})

        with fake_oauth_api(empty), pytest.raises(Exception, match="long-lived access token"):
            oauth.refresh_long_lived(ACCESS_TOKEN)

    def test_the_housekeeping_job_is_registered(self) -> None:
        """A sweep rather than a queue row scheduled at connect time: a queue row
        that fails five times is gone, and the channel then dies silently sixty
        days later."""
        from apps.queueing.housekeeping import housekeeping_jobs

        assert "refresh_instagram_tokens" in housekeeping_jobs()


class TestCredentialStorage:
    def test_a_stored_token_is_not_readable_from_the_column(self, instagram_connection: ChannelConnection) -> None:
        """``credentials`` is an EncryptedJSONField: the ciphertext is what the
        database holds, and the plaintext only ever comes back through the
        accessor (SECURITY-BASELINE §5)."""
        from django.db import connection as db

        with db.cursor() as cursor:
            cursor.execute(
                "SELECT credentials FROM channels_channel_connection WHERE id = %s",
                [str(instagram_connection.pk)],
            )
            (raw,) = cursor.fetchone()
        assert ACCESS_TOKEN not in str(raw)
        assert oauth.access_token(instagram_connection) == ACCESS_TOKEN

    def test_an_unparseable_expiry_reads_as_unknown(self, instagram_connection: ChannelConnection) -> None:
        instagram_connection.credentials = {"access_token": ACCESS_TOKEN, "token_expires_at": "2026-02-30T00:00"}  # type: ignore[assignment]
        instagram_connection.save(update_fields=["credentials", "updated_at"])
        assert oauth.token_expires_at(instagram_connection) is None

    def test_the_callback_url_comes_from_app_url_not_the_request(self, settings: Any) -> None:
        """Meta matches the redirect URI exactly, so a deployment behind a proxy
        must send the address the operator registered."""
        settings.APP_URL = "https://chat.example.test"
        assert oauth.callback_url() == "https://chat.example.test" + CALLBACK

    def test_the_authorize_url_carries_every_scope(self) -> None:
        url = oauth.authorize_url(client_id="123", state="s")
        for scope in oauth.SCOPES:
            assert scope in url
        assert json.dumps(oauth.callback_url())[1:-1] in url.replace("%3A", ":").replace("%2F", "/")
