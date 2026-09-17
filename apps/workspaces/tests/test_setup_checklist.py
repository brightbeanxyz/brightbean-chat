"""Home's first-run checklist tells the truth about what is set up.

Every step here reads live state rather than a stored onboarding flag, so the
only way it can be wrong is by asking the wrong question — which is what each
of these pins. A checklist that ticks a step the workspace has not actually
reached is worse than no checklist: it stops offering the thing still missing
and says, in the step's own words, that it is done.
"""

from typing import Any

import pytest
from django.urls import reverse

from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.flows.models import Trigger, TriggerType
from apps.flows.services import create_flow, publish, save_draft
from apps.members.roles import WorkspaceRole

pytestmark = pytest.mark.django_db


def _home(tenancy: Any, client_for: Any, user: Any = None) -> Any:
    url = reverse("workspaces:dashboard", kwargs={"workspace_id": tenancy.workspace.pk})
    return client_for(user or tenancy.owner).get(url)


def _steps(response: Any) -> dict[str, dict[str, Any]]:
    return {step["key"]: step for step in response.context["setup_steps"]}


def _connection(tenancy: Any, *, status: str) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform="telegram",
        display_name="@acme_bot",
        external_id="tg-1",
        status=status,
    )
    connection.rotate_webhook_secret()
    connection.save()
    return connection


class TestTheChannelStep:
    def test_an_active_connection_finishes_it(self, tenancy, client_for):
        _connection(tenancy, status=ConnectionStatus.ACTIVE)

        assert _steps(_home(tenancy, client_for))["channel"]["done"] is True

    @pytest.mark.parametrize("status", [ConnectionStatus.DISABLED, ConnectionStatus.NEEDS_REAUTH])
    def test_a_connection_that_cannot_deliver_does_not(self, tenancy, client_for, status):
        """The step's own done_body is "Messages arrive here now". A disabled or
        re-auth-pending connection receives nothing, so ticking it off hides the
        one action that would fix the workspace."""
        _connection(tenancy, status=status)

        assert _steps(_home(tenancy, client_for))["channel"]["done"] is False


class TestTheFlowStep:
    def _live_flow(self, tenancy: Any, *, with_trigger: bool) -> Any:
        flow = create_flow(workspace=tenancy.workspace, name="Welcome")
        save_draft(
            flow,
            {
                "schema": 1,
                "nodes": [
                    {
                        "id": "n1",
                        "type": "send_message",
                        "position": {"x": 0, "y": 0},
                        "config": {"blocks": [{"type": "text", "text": "hi"}]},
                    }
                ],
                "edges": [],
            },
        )
        if with_trigger:
            Trigger(
                workspace=tenancy.workspace,
                flow=flow,
                type=TriggerType.KEYWORD,
                config_json={"keywords": [{"text": "hi", "mode": "contains"}]},
                enabled=True,
            ).save()
        publish(flow)
        return flow

    def test_a_live_flow_with_an_enabled_trigger_finishes_it(self, tenancy, client_for):
        self._live_flow(tenancy, with_trigger=True)

        assert _steps(_home(tenancy, client_for))["flow"]["done"] is True

    def test_a_live_flow_nothing_can_reach_does_not(self, tenancy, client_for):
        """Publishing does not require a trigger — the builder reports "nothing
        starts this flow" as a warning and publishes anyway. Such a flow runs
        for nobody, so "A flow is answering for you." would be false."""
        self._live_flow(tenancy, with_trigger=False)

        assert _steps(_home(tenancy, client_for))["flow"]["done"] is False

    def test_a_trigger_switched_off_does_not_either(self, tenancy, client_for):
        flow = self._live_flow(tenancy, with_trigger=True)
        Trigger.objects.for_workspace(tenancy.workspace).filter(flow=flow).update(enabled=False)

        assert _steps(_home(tenancy, client_for))["flow"]["done"] is False


class TestTheCallsToAction:
    def test_only_the_current_step_offers_one(self, tenancy, client_for):
        """The list is ordered by dependency — a flow with no channel has
        nothing to reply on — so exactly one step is live at a time."""
        response = _home(tenancy, client_for)
        body = response.content.decode()
        steps = _steps(response)

        assert [key for key, step in steps.items() if step["current"]] == ["channel"]
        assert "Connect a channel" in body
        assert "Pick a template" not in body
        assert "Invite people" not in body

    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_member_who_cannot_act_is_not_offered_the_link(self, tenancy, client_for, role):
        """channels:list is gated on manage_channels, so the link an Agent saw
        went straight to a 403. The step still renders — what the workspace is
        missing is worth knowing — without the offer."""
        response = _home(tenancy, client_for, user=tenancy.user_for(role))

        assert _steps(response)["channel"]["actionable"] is False
        assert "Connect a channel" in response.content.decode()  # the title
        assert f'href="{reverse("channels:list", kwargs={"workspace_id": tenancy.workspace.pk})}"' not in (
            response.content.decode()
        )

    def test_an_admin_is(self, tenancy, client_for):
        response = _home(tenancy, client_for)

        assert _steps(response)["channel"]["actionable"] is True
        assert reverse("channels:list", kwargs={"workspace_id": tenancy.workspace.pk}) in response.content.decode()
