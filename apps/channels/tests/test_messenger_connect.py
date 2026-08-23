"""Facebook Login for Business, and the ``state`` that is its whole CSRF story.

    OAuth ``state`` tampering rejected; no token material in logs.

``TestStateIsTheBoundary`` is also the class that stands in for the IDOR sweep on
``/oauth/meta/callback/``. That route carries no tenant-shaped kwarg — Meta
whitelists one exact redirect URI per app, so the path cannot name a workspace —
and ``tests/idor.py`` therefore skips it the way it skips every workspace-neutral
route. What replaces the sweep's guarantee is here: the workspace comes from a
signed state, a state that is forged, expired, minted for another purpose or
minted for another workspace is refused, and a genuine state for a workspace the
signed-in user does not administer answers 404 rather than 403.

If this class is ever deleted, ``apps/channels/urls_oauth.py``'s docstring is
wrong and should go with it.
"""

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.channels import oauth_meta
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.channels.providers import meta_common
from apps.channels.providers.messenger import GET_STARTED_PAYLOAD, SUBSCRIBED_FIELDS
from apps.channels.tests.messenger_support import PAGE_TOKEN, Reply, fake_graph
from apps.channels.views_messenger import PENDING_SESSION_KEY
from apps.common import signing
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole
from tests.support import Tenancy, create_tenancy

pytestmark = pytest.mark.django_db

CALLBACK = "/oauth/meta/callback/"
# Assembled, not written out — see ``messenger_support.PAGE_TOKEN`` for why a
# credential-shaped literal in this repository fails CI on every open PR.
USER_TOKEN = "EAA" + "userTOKEN0123" * 3  # noqa: S105 - a fake credential for tests
GRANTED_PAGE = {"id": "555555555555555", "name": "Acme Support", "access_token": PAGE_TOKEN}


def connect_url(tenancy: Tenancy) -> str:
    return reverse("channels:messenger_connect", kwargs={"workspace_id": tenancy.workspace.pk})


def pages_url(tenancy: Tenancy) -> str:
    return reverse("channels:messenger_pages", kwargs={"workspace_id": tenancy.workspace.pk})


def graph_for_connect(pages: list[dict[str, Any]] | None = None) -> Any:
    """Configure the fake for a whole successful connect."""

    def configure(graph: Any) -> None:
        graph.reply("/oauth/access_token", Reply(body={"access_token": USER_TOKEN}))
        graph.reply("/me/accounts", Reply(body={"data": pages if pages is not None else [GRANTED_PAGE]}))

    return configure


def admin(tenancy: Tenancy, client_for: Any) -> Client:
    return client_for(tenancy.user_for(WorkspaceRole.ADMIN))


def complete_callback(client: Client, tenancy: Tenancy, **overrides: Any) -> Any:
    params = {"code": "a-real-code", "state": oauth_meta.mint_state(tenancy.workspace.pk), **overrides}
    with fake_graph(graph_for_connect()):
        return client.get(CALLBACK, params)


class TestPermissions:
    def test_an_admin_sees_the_setup_page(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        assert admin(tenancy, client_for).get(connect_url(tenancy)).status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_everyone_else_is_refused(self, tenancy: Tenancy, client_for: Any, role: str) -> None:
        response = client_for(tenancy.user_for(role)).get(connect_url(tenancy))
        assert response.status_code == 403

    def test_the_generic_add_a_channel_form_refuses_messenger(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """A row with no credentials is an active-looking channel whose every send fails.

        The refusal comes from ``registry.CONNECT_ROUTES`` through
        ``forms.clean_platform``, with no change to that form — which is what
        ``CONNECT_ROUTES`` is for.
        """
        response = admin(tenancy, client_for).post(
            reverse("channels:create", kwargs={"workspace_id": tenancy.workspace.pk}),
            {"platform": Platform.MESSENGER.value, "display_name": "Sneaky", "external_id": "123"},
        )
        assert response.status_code == 200
        assert b"guided setup" in response.content
        assert not ChannelConnection.objects.unscoped().filter(platform=Platform.MESSENGER).exists()


class TestStartingTheFlow:
    def test_it_sends_the_operator_to_facebook_with_a_signed_state(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response = admin(tenancy, client_for).post(connect_url(tenancy))
        assert response.status_code == 302

        query = parse_qs(urlparse(response["Location"]).query)
        assert query["client_id"] == ["1234567890"]
        assert query["redirect_uri"] == [oauth_meta.callback_url()]
        assert set(query["scope"][0].split(",")) == set(oauth_meta.SCOPES)
        assert oauth_meta.read_state(query["state"][0]) == str(tenancy.workspace.pk)

    def test_a_deployment_with_no_app_configured_says_so(self, tenancy: Tenancy, client_for: Any) -> None:
        response = admin(tenancy, client_for).post(connect_url(tenancy), follow=True)
        assert b"Settings" in response.content
        assert not ChannelConnection.objects.unscoped().filter(platform=Platform.MESSENGER).exists()


class TestStateIsTheBoundary:
    """The stand-in for the IDOR sweep on ``/oauth/meta/callback/``."""

    @pytest.mark.parametrize(
        "state",
        [
            "",
            "not-a-token",
            "eyJ3b3Jrc3BhY2UiOiAiMSJ9:fake:signature",
        ],
    )
    def test_a_state_we_did_not_mint_is_a_bare_404(
        self, state: str, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "c", "state": state})
        assert response.status_code == 404

    def test_a_state_minted_for_another_purpose_is_refused(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """``purpose`` is the signer's salt, so a token cannot be replayed here.

        This is exactly what ``apps.common.signing`` exists to provide, and why
        this flow does not reach for ``django.core.signing`` directly.
        """
        foreign = signing.sign({"workspace": str(tenancy.workspace.pk)}, purpose="unsubscribe")
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "c", "state": foreign})
        assert response.status_code == 404

    def test_an_expired_state_is_refused_indistinguishably(
        self, tenancy: Tenancy, client_for: Any, app_secret: str, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(oauth_meta, "STATE_MAX_AGE", -1)
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "c", "state": state})
        assert response.status_code == 404

    def test_a_tampered_workspace_breaks_the_signature(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        tampered = state[:-4] + ("aaaa" if not state.endswith("aaaa") else "bbbb")
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "c", "state": tampered})
        assert response.status_code == 404

    def test_a_genuine_state_for_another_tenants_workspace_answers_404(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """The check a valid signature alone would not give.

        A state captured from an admin's browser proves *we* minted it for that
        workspace, not that whoever is holding it belongs there. Without this, a
        stolen state would let anyone attach their own Facebook page to somebody
        else's workspace.

        404 rather than 403: a 403 would confirm the workspace exists, which over
        a UUID space is the only thing the caller was missing.
        """
        victim = create_tenancy("victim")
        state = oauth_meta.mint_state(victim.workspace.pk)
        response = admin(tenancy, client_for).get(CALLBACK, {"code": "c", "state": state})
        assert response.status_code == 404
        assert PENDING_SESSION_KEY not in admin(tenancy, client_for).session

    def test_a_member_without_manage_channels_is_refused(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """403 for a real member missing the permission — the decorator's answer."""
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        response = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).get(CALLBACK, {"code": "c", "state": state})
        assert response.status_code == 403

    def test_an_anonymous_caller_is_sent_to_log_in(self, tenancy: Tenancy, app_secret: str) -> None:
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        response = Client().get(CALLBACK, {"code": "c", "state": state})
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_a_declined_consent_screen_is_the_same_message_as_everything_else(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        response = client.get(CALLBACK, {"error": "access_denied", "state": state})
        assert response.status_code == 302
        assert PENDING_SESSION_KEY not in client.session


class TestChoosingAPage:
    def test_the_callback_stashes_the_user_token_encrypted(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """SECURITY-BASELINE §5: ``django_session.session_data`` is a plain column."""
        client = admin(tenancy, client_for)
        response = complete_callback(client, tenancy)
        assert response.status_code == 302
        assert response["Location"] == pages_url(tenancy)

        stashed = client.session[PENDING_SESSION_KEY]
        assert stashed["workspace"] == str(tenancy.workspace.pk)
        assert USER_TOKEN not in stashed["token"]
        from apps.common.encryption import decrypt_value

        assert decrypt_value(stashed["token"]) == USER_TOKEN

    def test_the_chooser_lists_pages_without_their_tokens(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        with fake_graph(graph_for_connect()):
            response = client.get(pages_url(tenancy))
        body = response.content.decode()
        assert "Acme Support" in body
        assert PAGE_TOKEN not in body

    def test_no_attempt_in_the_session_sends_the_operator_back_to_the_start(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        response = admin(tenancy, client_for).get(pages_url(tenancy))
        assert response.status_code == 302
        assert response["Location"] == connect_url(tenancy)

    def test_an_attempt_belonging_to_another_workspace_is_not_usable(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """A second tab, or a URL edited by hand."""
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        session = client.session
        session[PENDING_SESSION_KEY] = {**session[PENDING_SESSION_KEY], "workspace": "another-one"}
        session.save()

        response = client.get(pages_url(tenancy))
        assert response.status_code == 302

    def test_an_account_that_granted_no_pages_is_explained(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        with fake_graph(graph_for_connect(pages=[])):
            response = client.get(pages_url(tenancy))
        assert b"granted no pages" in response.content


class TestAnAbandonedAttemptLeavesNothingBehind:
    def test_opening_the_connect_page_again_drops_the_stashed_token(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """Nothing sweeps the session, so ``PENDING_MAX_AGE`` needs a trigger.

        Without this, an operator who finished Facebook's consent screen and then
        closed the tab left a live long-lived user token in ``django_session`` for
        the session's own 14-day life — while the Facebook token itself stays valid
        for about sixty days.
        """
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        assert PENDING_SESSION_KEY in client.session

        client.get(connect_url(tenancy))
        assert PENDING_SESSION_KEY not in client.session

    def test_starting_over_drops_it_too(self, tenancy: Tenancy, client_for: Any, app_secret: str) -> None:
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        client.post(connect_url(tenancy))
        assert PENDING_SESSION_KEY not in client.session

    def test_an_attempt_older_than_the_window_is_refused_and_cleared(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        session = client.session
        session[PENDING_SESSION_KEY] = {
            **session[PENDING_SESSION_KEY],
            "at": timezone.now().timestamp() - oauth_meta.STATE_MAX_AGE - 60,
        }
        session.save()

        response = client.get(pages_url(tenancy))
        assert response.status_code == 302
        assert PENDING_SESSION_KEY not in client.session


class TestConnectingThePage:
    def _choose(self, client: Client, tenancy: Tenancy, page_id: str = GRANTED_PAGE["id"]) -> Any:
        complete_callback(client, tenancy)
        with fake_graph(graph_for_connect()) as graph:
            response = client.post(pages_url(tenancy), {"page_id": page_id})
        return response, graph

    def test_it_stores_the_token_encrypted_and_wires_the_page_up(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        response, graph = self._choose(client, tenancy)
        assert response.status_code == 302

        connection = ChannelConnection.objects.unscoped().get(platform=Platform.MESSENGER)
        assert connection.external_id == GRANTED_PAGE["id"]
        assert connection.display_name == "Acme Support"
        assert connection.status == ConnectionStatus.ACTIVE
        assert meta_common.page_token(connection) == PAGE_TOKEN

        subscribe = next(call for call in graph.calls if call.matches("/subscribed_apps"))
        assert set(subscribe.params["subscribed_fields"].split(",")) == set(SUBSCRIBED_FIELDS)

        profile = next(call for call in graph.calls if call.matches("/messenger_profile"))
        assert profile.body == {"get_started": {"payload": GET_STARTED_PAYLOAD}}

    def test_the_stashed_token_is_dropped_once_the_page_is_connected(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        client = admin(tenancy, client_for)
        self._choose(client, tenancy)
        assert PENDING_SESSION_KEY not in client.session

    def test_the_raw_page_token_is_never_in_a_plain_column(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """``credentials`` is encrypted at rest; the row's own text must not leak it."""
        client = admin(tenancy, client_for)
        self._choose(client, tenancy)
        connection = ChannelConnection.objects.unscoped().get(platform=Platform.MESSENGER)
        stored = ChannelConnection.all_objects.filter(pk=connection.pk).values("credentials").get()
        assert PAGE_TOKEN not in stored["credentials"]

    def test_a_page_the_account_does_not_administer_is_refused(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """A hand-crafted POST naming somebody else's page id.

        One answer for "you picked nothing" and "that page is not yours": naming
        which would confirm whether a given page id is real.
        """
        client = admin(tenancy, client_for)
        response, _graph = self._choose(client, tenancy, page_id="999999999999999")
        assert response.status_code == 200
        assert b"Pick one of the pages below" in response.content
        assert not ChannelConnection.objects.unscoped().filter(platform=Platform.MESSENGER).exists()

    def test_a_page_already_connected_elsewhere_is_refused_without_saying_where(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """SPEC §5's unique ``(platform, external_id)`` is deployment-wide."""
        other = create_tenancy("rival")
        ChannelConnection.objects.create(
            workspace=other.workspace,
            platform=Platform.MESSENGER.value,
            display_name="Rival's copy",
            external_id=GRANTED_PAGE["id"],
        )
        client = admin(tenancy, client_for)
        response, _graph = self._choose(client, tenancy)
        assert response.status_code == 200
        body = response.content.decode()
        assert "already connected to this deployment" in body
        assert "rival" not in body.lower()

    def test_a_page_meta_will_not_subscribe_leaves_no_connection_behind(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """A page Meta will not deliver for is not a connection.

        One left in the list looking connected while nothing ever arrives is the
        worse outcome — the trade ``views_telegram._connect`` makes for
        ``setWebhook``.
        """

        def configure(graph: Any) -> None:
            graph_for_connect()(graph)
            graph.reply("/subscribed_apps", Reply(status=400))

        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        with fake_graph(configure):
            response = client.post(pages_url(tenancy), {"page_id": GRANTED_PAGE["id"]})
        assert response.status_code == 200
        assert b"would not start sending" in response.content
        assert not ChannelConnection.objects.unscoped().filter(platform=Platform.MESSENGER).exists()

    def test_a_failed_get_started_button_keeps_the_connection_and_warns(
        self, tenancy: Tenancy, client_for: Any, app_secret: str
    ) -> None:
        """Everything except SPEC §10's welcome trigger works without it.

        Deleting a working channel over a button would be the wrong trade, so the
        connection stands and the operator is told what is missing.
        """

        def configure(graph: Any) -> None:
            graph_for_connect()(graph)
            graph.reply("/messenger_profile", Reply(status=400))

        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        with fake_graph(configure):
            response = client.post(pages_url(tenancy), {"page_id": GRANTED_PAGE["id"]}, follow=True)
        assert ChannelConnection.objects.unscoped().filter(platform=Platform.MESSENGER).exists()
        assert b"Get Started button could not be configured" in response.content


class TestNothingLogsTheToken:
    def test_a_whole_connect_leaves_no_credential_in_the_log(
        self, tenancy: Tenancy, client_for: Any, app_secret: str, caplog: Any
    ) -> None:
        """SECURITY-BASELINE §5, asserted over the *captured* records.

        ``apps.common.logging``'s record factory scrubs at creation, so this sees
        what a handler would — including anything a library logged on our behalf.
        """
        caplog.set_level(logging.DEBUG)
        client = admin(tenancy, client_for)
        complete_callback(client, tenancy)
        with fake_graph(graph_for_connect()):
            client.post(pages_url(tenancy), {"page_id": GRANTED_PAGE["id"]})

        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert PAGE_TOKEN not in blob
        assert USER_TOKEN not in blob
        assert app_secret not in blob

    def test_a_failed_exchange_logs_no_detail(
        self, tenancy: Tenancy, client_for: Any, app_secret: str, caplog: Any
    ) -> None:
        def configure(graph: Any) -> None:
            graph.reply("/oauth/access_token", Reply(status=400))

        caplog.set_level(logging.DEBUG)
        client = admin(tenancy, client_for)
        state = oauth_meta.mint_state(tenancy.workspace.pk)
        with fake_graph(configure):
            client.get(CALLBACK, {"code": "a-real-code", "state": state})

        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert app_secret not in blob
        assert "a-real-code" not in blob
