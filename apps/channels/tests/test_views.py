"""Workspace settings → Channels (SECURITY-BASELINE §§1, 5).

The cross-tenant sweep itself lives in ``tests/test_idor.py``, which walks every
registered route automatically once ``connection_id`` has a resolver. What is
here is what that sweep cannot see: the permission matrix, and the handling of
the secret.
"""

import logging
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.channels.models import ChannelConnection
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def url_for(name: str, tenancy: Any, connection: ChannelConnection | None = None) -> str:
    kwargs: dict[str, Any] = {"workspace_id": tenancy.workspace.pk}
    if connection is not None:
        kwargs["connection_id"] = connection.pk
    return reverse(f"channels:{name}", kwargs=kwargs)


class TestPermissions:
    """``manage_channels`` is admin-only (apps.members.roles._ADMIN_ONLY_KEYS)."""

    def test_an_admin_can_see_the_list(self, tenancy: Any, client_for: Any) -> None:
        response = client_for(tenancy.user_for(WorkspaceRole.ADMIN)).get(url_for("list", tenancy))
        assert response.status_code == 200

    @pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_everyone_else_is_refused(self, tenancy: Any, client_for: Any, role: str) -> None:
        # 403, not 404: "you are in this workspace but lack the permission"
        # reveals nothing the caller did not know (CONTRIBUTING).
        assert client_for(tenancy.user_for(role)).get(url_for("list", tenancy)).status_code == 403

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, tenancy: Any) -> None:
        response = Client().get(url_for("list", tenancy))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    @pytest.mark.parametrize("name", ["set_status", "rotate_secret", "delete"])
    def test_write_routes_reject_a_get(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection, name: str
    ) -> None:
        client = client_for(tenancy.user_for(WorkspaceRole.ADMIN))
        assert client.get(url_for(name, tenancy, connection)).status_code == 405

    @pytest.mark.parametrize("name", ["set_status", "rotate_secret", "delete"])
    def test_write_routes_refuse_a_non_admin_before_the_method_check(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection, name: str
    ) -> None:
        """The stacking convention: permission first, so a GET answers 403 not 405."""
        client = client_for(tenancy.user_for(WorkspaceRole.EDITOR))
        assert client.get(url_for(name, tenancy, connection)).status_code == 403


class TestCreate:
    def test_creating_a_connection_shows_its_secret_once(self, tenancy: Any, client_for: Any) -> None:
        client = client_for(tenancy.owner)
        response = client.post(
            url_for("create", tenancy),
            {"platform": Platform.TELEGRAM, "display_name": "Support bot", "external_id": "bot-123"},
        )
        assert response.status_code == 200

        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        body = response.content.decode()
        assert connection.webhook_secret in body
        assert "not shown again" in body

    def test_the_secret_never_appears_again(self, tenancy: Any, client_for: Any, connection: Any) -> None:
        client = client_for(tenancy.owner)
        for page in (url_for("list", tenancy), url_for("detail", tenancy, connection)):
            assert connection.webhook_secret not in client.get(page).content.decode()

    def test_the_secret_does_not_travel_through_the_session(self, tenancy: Any, client_for: Any) -> None:
        """django.contrib.messages is a database table in this project."""
        client = client_for(tenancy.owner)
        client.post(
            url_for("create", tenancy),
            {"platform": Platform.TELEGRAM, "display_name": "Bot", "external_id": "bot-123"},
        )
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        stored_session = "".join(str(value) for value in client.session.items())
        assert connection.webhook_secret not in stored_session

    def test_the_secret_never_reaches_a_log(self, tenancy: Any, client_for: Any, caplog: Any) -> None:
        with caplog.at_level(logging.DEBUG):
            client_for(tenancy.owner).post(
                url_for("create", tenancy),
                {"platform": Platform.TELEGRAM, "display_name": "Bot", "external_id": "bot-123"},
            )
        connection = ChannelConnection.objects.for_workspace(tenancy.workspace).get()
        assert connection.webhook_secret not in caplog.text

    def test_a_duplicate_account_is_refused_without_naming_the_other_workspace(
        self, tenancy: Any, other_tenancy: Any, client_for: Any
    ) -> None:
        """SPEC §5's constraint is deployment-wide; the message must not leak across it."""
        ChannelConnection.objects.create(
            workspace=other_tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="Rival bot",
            external_id="contested",
        )
        response = client_for(tenancy.owner).post(
            url_for("create", tenancy),
            {"platform": Platform.TELEGRAM, "display_name": "Mine", "external_id": "contested"},
        )
        body = response.content.decode()

        assert response.status_code == 200
        assert "already connected to this deployment" in body
        assert other_tenancy.workspace.name not in body
        assert "Rival bot" not in body
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()

    def test_the_new_connection_belongs_to_the_current_workspace(
        self, tenancy: Any, other_tenancy: Any, client_for: Any
    ) -> None:
        """A posted workspace field must not be able to redirect ownership."""
        client_for(tenancy.owner).post(
            url_for("create", tenancy),
            {
                "platform": Platform.TELEGRAM,
                "display_name": "Bot",
                "external_id": "bot-123",
                "workspace": str(other_tenancy.workspace.pk),
            },
        )
        connection = ChannelConnection.objects.unscoped().get(external_id="bot-123")
        assert connection.workspace_id == tenancy.workspace.pk


class TestLifecycle:
    def test_disable_and_enable(self, tenancy: Any, client_for: Any, connection: ChannelConnection) -> None:
        client = client_for(tenancy.owner)
        client.post(url_for("set_status", tenancy, connection), {"status": "disabled"})
        connection.refresh_from_db()
        assert connection.status == "disabled"

        client.post(url_for("set_status", tenancy, connection), {"status": "active"})
        connection.refresh_from_db()
        assert connection.status == "active"

    @pytest.mark.parametrize("status", ["needs_reauth", "", "nonsense", "deleted"])
    def test_only_the_two_operator_settable_statuses_are_accepted(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection, status: str
    ) -> None:
        client_for(tenancy.owner).post(url_for("set_status", tenancy, connection), {"status": status})
        connection.refresh_from_db()
        assert connection.status == "active"

    def test_rotating_shows_a_new_secret_and_invalidates_the_old(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection
    ) -> None:
        old_secret = connection.webhook_secret
        response = client_for(tenancy.owner).post(url_for("rotate_secret", tenancy, connection))

        connection.refresh_from_db()
        assert response.status_code == 200
        assert connection.webhook_secret != old_secret
        assert connection.webhook_secret in response.content.decode()
        assert ChannelConnection.resolve_by_webhook_secret(old_secret) is None

    def test_deleting_removes_the_connection(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection
    ) -> None:
        client_for(tenancy.owner).post(url_for("delete", tenancy, connection))
        assert not ChannelConnection.objects.for_workspace(tenancy.workspace).exists()


class TestListing:
    def test_only_this_workspaces_connections_are_listed(
        self, tenancy: Any, other_tenancy: Any, client_for: Any, connection: ChannelConnection
    ) -> None:
        theirs = ChannelConnection.objects.create(
            workspace=other_tenancy.workspace,
            platform=Platform.MESSENGER,
            display_name="Rival page",
            external_id="rival-page",
        )
        body = client_for(tenancy.owner).get(url_for("list", tenancy)).content.decode()
        assert connection.display_name in body
        assert theirs.display_name not in body

    def test_the_webhook_url_is_shown(self, tenancy: Any, client_for: Any, connection: ChannelConnection) -> None:
        body = client_for(tenancy.owner).get(url_for("list", tenancy)).content.decode()
        assert "/webhooks/telegram/" in body

    def test_a_hostile_display_name_is_escaped(self, tenancy: Any, client_for: Any) -> None:
        ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="<script>alert(1)</script>",
            external_id="xss-bot",
        )
        body = client_for(tenancy.owner).get(url_for("list", tenancy)).content.decode()
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body

    def test_capability_information_comes_from_the_static_table(
        self, tenancy: Any, client_for: Any, connection: ChannelConnection
    ) -> None:
        """No adapter is registered, and the detail page is still complete."""
        body = client_for(tenancy.owner).get(url_for("detail", tenancy, connection)).content.decode()
        assert "4096" in body  # Telegram's max_text_len
        assert "Messaging window" in body
