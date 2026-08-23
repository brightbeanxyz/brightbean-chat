"""Role gating, an acceptance criterion of issue #14.

    Role checks: Viewer read-only; Agent has no access to flows/settings routes.
    IDOR fuzz on all endpoints.

The fuzz half lives in ``tests/idor.py``, which walks the URLconf and hits every
route naming a tenant object with another tenant's ids; issue #14 registered
``conversation_id`` and ``message_id`` there and pinned its route names in
``tests/test_idor.py``. What is here is the other half — the roles — plus the
sharper cross-tenant case the sweep cannot reach: the attacker's *own* workspace
id paired with the victim's conversation id, where only
``get_scoped_object_or_404`` stands in the way.
"""

from typing import Any

import pytest
from django.urls import reverse

from apps.members.roles import ROLE_PERMISSIONS, WorkspaceRole
from apps.messaging.models import Conversation

pytestmark = pytest.mark.django_db

#: Every route the inbox writes through, with a minimal valid payload.
WRITE_ROUTES: tuple[tuple[str, dict[str, str]], ...] = (
    ("send", {"body": "hello"}),
    ("note", {"body": "hello"}),
    ("assign", {"assignee": ""}),
    ("state", {"state": "done"}),
    ("pause", {"action": "pause"}),
    ("stop", {}),
    ("tags", {"tag": ""}),
)

#: Every route the inbox reads through, minus the two needing extra kwargs.
READ_ROUTES = ("thread", "messages", "composer", "header", "sidebar")


class TestTheRoleTableItself:
    def test_viewer_may_read_the_inbox_but_not_reply(self) -> None:
        """SPEC §4.2's table, asserted here so a change to it fails beside the
        views that depend on it rather than only in members' own suite."""
        viewer = ROLE_PERMISSIONS[WorkspaceRole.VIEWER]

        assert viewer["use_inbox"] is True
        assert viewer["reply_in_inbox"] is False

    def test_agent_is_the_floor_for_replying_and_assigning(self) -> None:
        agent = ROLE_PERMISSIONS[WorkspaceRole.AGENT]

        assert agent["reply_in_inbox"] is True
        assert agent["edit_contact_fields"] is True
        assert agent["edit_flows"] is False
        assert agent["manage_workspace_settings"] is False


class TestAViewer:
    def test_can_open_every_read_surface(self, viewer_client: Any, url_for: Any, conversation: Conversation) -> None:
        assert viewer_client.get(url_for("list")).status_code == 200
        assert viewer_client.get(url_for("rows")).status_code == 200
        for name in READ_ROUTES:
            response = viewer_client.get(url_for(name, conversation_id=conversation.pk))
            assert response.status_code == 200, name

    @pytest.mark.parametrize(("name", "payload"), WRITE_ROUTES)
    def test_cannot_reach_any_write(
        self, name: str, payload: dict[str, str], viewer_client: Any, url_for: Any, conversation: Conversation
    ) -> None:
        """403, not 404: the caller is inside this workspace and already knows
        the conversation exists, so hiding it would tell them nothing they did
        not have (SECURITY-BASELINE §1)."""
        response = viewer_client.post(url_for(name, conversation_id=conversation.pk), payload)

        assert response.status_code == 403, name

    def test_cannot_retry_a_failed_send(
        self, viewer_client: Any, url_for: Any, conversation: Conversation, outbound: Any
    ) -> None:
        from apps.messaging.models import MessageStatus

        failed = outbound("no", status=MessageStatus.FAILED, error="opted_out")

        response = viewer_client.post(url_for("retry", conversation_id=conversation.pk, message_id=failed.pk))

        assert response.status_code == 403

    def test_the_thread_offers_no_controls(self, viewer_client: Any, url_for: Any, conversation: Conversation) -> None:
        """Hidden rather than rendered-and-refused, the same way every other
        page in this codebase gates a control it would reject."""
        pane = viewer_client.get(
            url_for("thread", conversation_id=conversation.pk), headers={"HX-Request": "true"}
        ).content.decode()

        assert "Read only" in pane
        assert "Mark done" not in pane
        assert 'name="body"' not in pane


class TestAnAgent:
    def test_can_reach_every_inbox_write(
        self, agent_client: Any, url_for: Any, conversation: Conversation, identity: Any
    ) -> None:
        from apps.channels.tests.fake_adapter import registered
        from apps.common.platforms import Platform

        with registered(Platform.TELEGRAM):
            for name, payload in WRITE_ROUTES:
                response = agent_client.post(url_for(name, conversation_id=conversation.pk), payload)
                assert response.status_code == 204, name

    def test_has_no_access_to_the_flow_builder(self, tenancy: Any, agent_client: Any) -> None:
        """`edit_flows` is editor-and-up, and an inbox agent holding it would be
        the failure this acceptance criterion is written to catch."""
        from apps.flows.services import create_flow

        flow = create_flow(workspace=tenancy.workspace, name="Onboarding")

        response = agent_client.post(
            reverse("flows:rename", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk}),
            {"name": "Renamed"},
        )

        assert response.status_code == 403

    def test_has_no_access_to_workspace_settings(self, tenancy: Any, agent_client: Any) -> None:
        response = agent_client.get(reverse("channels:list", kwargs={"workspace_id": tenancy.workspace.pk}))

        assert response.status_code == 403


class TestCrossTenantAccess:
    @pytest.mark.parametrize("name", READ_ROUTES)
    def test_a_victims_conversation_under_the_attackers_own_workspace_is_a_404(
        self, name: str, other_tenancy: Any, client_for: Any, conversation: Conversation
    ) -> None:
        """The case the URL-walking sweep cannot construct: a fully privileged
        member of another org, using *their own* workspace id, pointing at
        somebody else's conversation. RBACMiddleware is satisfied — the
        workspace is theirs — so the only thing left is the scoped fetch."""
        attacker = client_for(other_tenancy.user_for("admin"))
        url = reverse(
            f"inbox:{name}",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "conversation_id": conversation.pk},
        )

        assert attacker.get(url).status_code == 404

    @pytest.mark.parametrize(("name", "payload"), WRITE_ROUTES)
    def test_no_write_reaches_across_either(
        self,
        name: str,
        payload: dict[str, str],
        other_tenancy: Any,
        client_for: Any,
        conversation: Conversation,
    ) -> None:
        attacker = client_for(other_tenancy.user_for("admin"))
        url = reverse(
            f"inbox:{name}",
            kwargs={"workspace_id": other_tenancy.workspace.pk, "conversation_id": conversation.pk},
        )

        assert attacker.post(url, payload).status_code == 404

    def test_an_anonymous_visitor_is_sent_to_log_in(self, client: Any, url_for: Any) -> None:
        response = client.get(url_for("list"))

        assert response.status_code == 302
        assert "/accounts/login/" in response.headers["Location"]
