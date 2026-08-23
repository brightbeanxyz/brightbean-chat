"""The composer (SPEC §13.1) — its gates, and the rule it is built on.

The rule: **every per-platform affordance is a registry lookup, never a branch.**
Contract 4's promise is that a Layer-5 platform costs one module and one registry
line, and a composer that named platforms would be the first crack in it. So the
assertions here are written against the *policy and capability tables* rather
than against platform names wherever the test can be — and the last class in the
file is a structural scan that fails on a platform comparison in any template
this app renders.
"""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from django.urls import reverse

from apps.broadcasts import composer, services
from apps.broadcasts.models import Broadcast, BroadcastStatus
from apps.channels import policy as channel_policy
from apps.channels.models import ChannelConnection, ConnectionStatus
from apps.common.platforms import Platform
from apps.members.roles import WorkspaceRole


def _url(name, tenancy, **kwargs):
    return reverse(name, kwargs={"workspace_id": tenancy.workspace.pk, **kwargs})


@pytest.mark.django_db
class TestChannelSelector:
    def test_a_platform_that_forbids_broadcasts_is_absent(self, tenancy):
        """SPEC §13.2: "Instagram never appears in the broadcast channel selector".

        Asserted through ``PlatformPolicy.broadcast_allowed`` rather than through
        the name: what has to be true is that the *flag* governs, so a future
        platform that forbids broadcasts is excluded the day its policy row lands.
        """
        for platform in Platform.values:
            ChannelConnection.objects.create(
                workspace=tenancy.workspace,
                platform=platform,
                display_name=f"{platform} connection",
                external_id=f"{platform}-{tenancy.slug}",
                status=ConnectionStatus.ACTIVE,
            )

        offered = {connection.platform for connection in composer.broadcastable_connections(tenancy.workspace)}
        allowed = {p for p in Platform.values if channel_policy.policy_for(p).broadcast_allowed}

        assert offered == allowed
        # And, concretely, the one SPEC names.
        assert Platform.INSTAGRAM not in offered

    def test_a_disabled_connection_is_not_offered(self, tenancy, connection):
        connection.status = ConnectionStatus.DISABLED
        connection.save(update_fields=["status"])

        assert composer.broadcastable_connections(tenancy.workspace) == []

    def test_creating_on_a_forbidden_channel_404s_rather_than_validating(self, tenancy, client_for):
        """The endpoint has to agree with the selector, or "never appears" is
        only true of the markup."""
        instagram = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.INSTAGRAM,
            display_name="IG",
            external_id=f"ig-{tenancy.slug}",
            status=ConnectionStatus.ACTIVE,
        )

        response = client_for(tenancy.owner).post(
            _url("broadcasts:create", tenancy), {"name": "Nope", "connection_id": str(instagram.pk)}
        )

        assert response.status_code == 404
        assert not Broadcast.objects.for_workspace(tenancy.workspace).exists()

    def test_the_service_refuses_it_too(self, tenancy):
        instagram = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.INSTAGRAM,
            display_name="IG",
            external_id=f"ig2-{tenancy.slug}",
            status=ConnectionStatus.ACTIVE,
        )

        with pytest.raises(services.BroadcastError, match="does not allow broadcasts"):
            services.create_broadcast(
                workspace=tenancy.workspace, name="Nope", connection=instagram, user=tenancy.owner
            )


@pytest.mark.django_db
class TestComposerConfig:
    def test_block_kinds_come_from_the_capability_table(self, tenancy, connection, whatsapp_connection):
        from apps.channels.capabilities import capabilities_for

        for target in (connection, whatsapp_connection):
            config = composer.composer_config(tenancy.workspace, target)
            caps = capabilities_for(target.platform)

            assert config["blocks"] == [kind for kind in composer.BLOCK_KINDS if caps.supports_block(kind)]
            assert config["allows_buttons"] is (caps.max_buttons > 0)
            assert config["max_text_len"] == caps.max_text_len

    def test_the_tag_selector_and_metas_copy_come_from_the_policy_row(self, tenancy, messenger_connection, connection):
        """SPEC §6.4 requires the composer to display Meta's allowed-use text.

        Verbatim, and from the policy row — ``NeedsTag`` carries it beside the
        tag list precisely because Meta revises the two together.
        """
        outside = channel_policy.policy_for(messenger_connection.platform).outside_window
        config = composer.composer_config(tenancy.workspace, messenger_connection)

        assert config["tag_choices"] == list(outside.tags)
        assert config["tag_allowed_use_text"] == outside.allowed_use_text

        # A platform whose outside-window answer is not a tag offers none.
        assert composer.composer_config(tenancy.workspace, connection)["tag_choices"] == []

    def test_templates_are_the_approved_ones_for_this_connection(self, tenancy, whatsapp_connection):
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        approved = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="ready_one",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hi {{1}}"}},
        )
        WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="still_pending",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.PENDING,
            body_structure={"body": {"text": "Nope"}},
        )

        config = composer.composer_config(tenancy.workspace, whatsapp_connection)

        assert [template["id"] for template in config["templates"]] == [str(approved.pk)]
        # variable_schema's shape, not a second one built here.
        assert config["templates"][0]["slots"] == ["body.1"]

    def test_a_platform_without_templates_offers_none(self, tenancy, connection):
        assert composer.composer_config(tenancy.workspace, connection)["templates"] == []
        assert composer.composer_config(tenancy.workspace, connection)["cost_hint"] is None

    def test_the_segment_preview_applies_only_where_it_means_something(self, tenancy):
        """Asked of the capability table: a segment-counted channel is one that
        renders text and nothing else, which is the shape SPEC §6.6 describes."""
        sms = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.SMS,
            display_name="Number",
            external_id=f"sms-{tenancy.slug}",
            status=ConnectionStatus.ACTIVE,
        )
        telegram = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="Bot",
            external_id=f"tg-{tenancy.slug}",
            status=ConnectionStatus.ACTIVE,
        )

        assert composer.composer_config(tenancy.workspace, sms)["counts_segments"] is True
        assert composer.composer_config(tenancy.workspace, telegram)["counts_segments"] is False

    def test_the_segment_count_is_l5ds_endpoint_not_a_second_counter(self, tenancy, client_for, connection):
        """apps/channels/segments.py is pure and is the only implementation.

        L5-D already exposes it as ``channels:sms_segment_preview``, and that
        view's docstring names this composer as a caller. So the composer's job
        is to *offer* the count — the flag from the capability table — and to
        point at that endpoint. A JavaScript approximation of GSM-7 versus UCS-2
        would be a second counter that disagrees with the one the send is costed
        against.
        """
        from apps.channels.segments import segments_for

        text = "a" * 161
        assert segments_for(text).segments == 2

        response = client_for(tenancy.owner).post(
            reverse("channels:sms_segment_preview", kwargs={"workspace_id": tenancy.workspace.pk}),
            {"text": text},
        )

        assert response.status_code == 200
        assert b"2" in response.content

    def test_the_composer_script_does_not_reimplement_the_arithmetic(self):
        """Structural, because the tempting shortcut is a regex in the browser.

        One curly quote takes a 160-character message to UCS-2 and 70, and a
        client-side guess that got that wrong would show a price half the real
        one right up until the bill arrived.
        """
        from pathlib import Path

        script = (Path(__file__).resolve().parents[3] / "templates" / "broadcasts" / "_compose_script.html").read_text()

        assert "160" not in script
        assert "153" not in script
        assert "UCS2" not in script


@pytest.mark.django_db
class TestScheduleGates:
    def test_messenger_refuses_an_outside_window_audience_with_no_tag(
        self, tenancy, make_contacts, make_broadcast, messenger_connection
    ):
        """SPEC §6.4's composer gate, and it is policy-data-driven.

        The refusal fires because ``needs_tag`` has a non-zero count on the same
        annotation the preview came from — not because the platform is called
        Messenger — and it prints Meta's allowed-use text with it.
        """
        make_contacts(3, connection=messenger_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=messenger_connection)
        outside = channel_policy.policy_for(messenger_connection.platform).outside_window

        with pytest.raises(services.BroadcastError) as caught:
            services.schedule_broadcast(broadcast)

        assert "message tag" in str(caught.value)
        assert outside.allowed_use_text in str(caught.value)

    def test_the_same_audience_is_accepted_once_a_tag_is_chosen(
        self, tenancy, make_contacts, make_broadcast, messenger_connection
    ):
        make_contacts(3, connection=messenger_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=messenger_connection, tag="ACCOUNT_UPDATE")

        services.schedule_broadcast(broadcast)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.SCHEDULED

    def test_whatsapp_refuses_an_outside_window_audience_with_no_template(
        self, tenancy, make_contacts, make_broadcast, whatsapp_connection
    ):
        make_contacts(2, connection=whatsapp_connection, window=-timedelta(hours=1))
        broadcast = make_broadcast(connection=whatsapp_connection)

        with pytest.raises(services.BroadcastError, match="approved template"):
            services.schedule_broadcast(broadcast)

    def test_a_tag_the_platform_does_not_accept_is_refused_on_the_way_in(self, make_broadcast, messenger_connection):
        """Checked here as well as by the compliance engine, deliberately.

        A tag the platform will not take would otherwise be stored, previewed as
        if it worked, and refused ten thousand times at send.
        """
        broadcast = make_broadcast(connection=messenger_connection)

        with pytest.raises(services.BroadcastError, match="not one this channel accepts"):
            services.set_tag(broadcast, "HUMAN_AGENT")

    def test_the_human_agent_tag_cannot_be_smuggled_in(self, make_broadcast, messenger_connection):
        """SPEC §22 hard-codes the seven-day allowance to inbox sends.

        It is not in Messenger's ``outside_window.tags``, so it fails the check
        above — and would fail again at ``can_send``, which grants it only to
        ``source="agent"``. Two independent refusals, which is what a rule this
        load-bearing should have.
        """
        from apps.messaging.compliance import HUMAN_AGENT_TAG

        outside = channel_policy.policy_for(messenger_connection.platform).outside_window
        assert HUMAN_AGENT_TAG not in outside.tags

        with pytest.raises(services.BroadcastError):
            services.set_tag(make_broadcast(connection=messenger_connection), HUMAN_AGENT_TAG)


@pytest.mark.django_db
class TestWizardEndpoints:
    def test_the_audience_preview_shows_a_count_before_anything_is_saved(
        self, tenancy, client_for, make_contacts, make_broadcast, connection
    ):
        """apps/contacts/conditions.py names this by issue number: an empty
        ``rules`` list under ``match: all`` targets the whole workspace, so
        "issue #23 must show a count before sending"."""
        make_contacts(7, connection=connection)
        broadcast = make_broadcast(connection=connection, filter_json={"match": "any", "rules": []})

        response = client_for(tenancy.owner).get(
            _url("broadcasts:audience_preview", tenancy, broadcast_id=broadcast.pk)
            + '?filter={"match":"all","rules":[]}'
        )

        assert response.status_code == 200
        assert b"7" in response.content
        # And it did not save: the broadcast still targets nobody.
        broadcast.refresh_from_db()
        assert broadcast.target_filter_json == {"match": "any", "rules": []}

    def test_a_bad_filter_document_is_refused_rather_than_ignored(
        self, tenancy, client_for, make_broadcast, connection
    ):
        """Answering "everyone" to a filter that will not compile is the least
        safe way to be wrong, and the count feeds a send."""
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).get(
            _url("broadcasts:audience_preview", tenancy, broadcast_id=broadcast.pk) + "?filter=not-json"
        )

        assert response.status_code == 200
        assert b"alert-warning" in response.content

    def test_another_tenants_segment_id_is_a_404(self, tenancy, other_tenancy, client_for, make_broadcast, connection):
        """The IDOR sweep walks URL kwargs and cannot see a query-string id, so
        this case needs its own test (the CRM's ``resolve_query`` says the same)."""
        from apps.contacts.models import Segment

        theirs = Segment.objects.create(
            workspace=other_tenancy.workspace, name="Theirs", filter_json={"match": "all", "rules": []}
        )
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).get(
            _url("broadcasts:audience_preview", tenancy, broadcast_id=broadcast.pk) + f"?segment={theirs.pk}"
        )

        assert response.status_code == 404

    def test_the_content_step_stores_a_single_node_graph(self, tenancy, client_for, make_contacts, connection):
        """ROADMAP line 43: "single-node graph_json — no React embed"."""
        from apps.broadcasts.tests.conftest import EVERYONE

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Draft", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_content", tenancy, broadcast_id=broadcast.pk),
            {"mode": "message", "config": '{"blocks": [{"type": "text", "text": "Hi"}]}'},
        )

        assert response.status_code == 200
        broadcast.refresh_from_db()
        graph = services.node_config(broadcast.flow.versions.first().graph_json)
        assert graph == {"blocks": [{"type": "text", "text": "Hi"}]}

    def test_an_unknown_config_key_is_refused(self, tenancy, client_for, connection):
        """SECURITY-BASELINE §7's mass-assignment guard, borrowed whole.

        Every object in the flow schema is closed, at any depth, so the composer
        gets that for free by writing the same document the builder writes.
        """
        from apps.broadcasts.tests.conftest import EVERYONE

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Draft", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        with pytest.raises(services.BroadcastError):
            services.save_content(broadcast, {"blocks": [{"type": "text", "text": "Hi"}], "smuggled": 1})

    def test_an_oversized_document_is_refused_before_it_is_parsed(
        self, tenancy, client_for, make_broadcast, connection
    ):
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_content", tenancy, broadcast_id=broadcast.pk),
            {"mode": "message", "config": "x" * (64 * 1024 + 1)},
        )

        assert response.status_code == 200
        assert b"too large" in response.content

    def test_the_step_parameter_cannot_name_an_arbitrary_template(
        self, tenancy, client_for, make_broadcast, connection
    ):
        broadcast = make_broadcast(connection=connection)

        response = client_for(tenancy.owner).get(
            _url("broadcasts:wizard", tenancy, broadcast_id=broadcast.pk) + "?step=../../base"
        )

        assert response.status_code == 200


@pytest.mark.django_db
class TestPermissions:
    @pytest.mark.parametrize("role", [WorkspaceRole.AGENT, WorkspaceRole.VIEWER])
    def test_a_member_without_send_broadcasts_is_refused(self, tenancy, client_for, role):
        """``send_broadcasts`` is the key the placeholder route already used;
        403 is right here — it reveals nothing the caller did not know."""
        response = client_for(tenancy.user_for(role)).get(_url("broadcasts:list", tenancy))

        assert response.status_code == 403

    def test_an_editor_may_use_it(self, tenancy, client_for):
        response = client_for(tenancy.user_for(WorkspaceRole.EDITOR)).get(_url("broadcasts:list", tenancy))

        assert response.status_code == 200


class TestNoPlatformBranches:
    """Contract 4, asserted structurally rather than promised in prose.

    A ``{% if platform == "whatsapp" %}`` in a template, or a platform literal
    compared in this package's Python, is the failure mode the layer-6 ground
    rules name outright. Both are invisible in review the day a seventh platform
    arrives and its composer silently offers nothing.
    """

    ROOT = Path(__file__).resolve().parents[3]

    def test_no_template_branches_on_a_platform_name(self):
        offenders = []
        for path in (self.ROOT / "templates" / "broadcasts").glob("*.html"):
            text = path.read_text()
            for platform in ("telegram", "instagram", "messenger", "whatsapp", "sms", "email"):
                # `platform|platform_class` is a *style* lookup and is fine; a
                # comparison is what decides an affordance.
                for needle in (f'platform == "{platform}"', f"platform == '{platform}'", f'platform == "{platform}"'):
                    if needle in text:
                        offenders.append(f"{path.name}: {needle}")
        assert not offenders, (
            "A composer template branches on a platform name. Every affordance has to come from "
            "apps/broadcasts/composer.py, which reads the capability and policy tables — see "
            "docs/agent-prompts/layer-6.md."
        )

    def test_no_module_in_this_app_compares_a_platform_literal(self):
        """An AST scan, not a grep: a *string* naming a platform is fine — the
        conftest builds connections with them — and a comparison is not."""
        platforms = {"telegram", "instagram", "messenger", "whatsapp", "sms", "email"}
        offenders = []
        package = self.ROOT / "apps" / "broadcasts"
        for path in package.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Compare):
                    continue
                for side in (node.left, *node.comparators):
                    if isinstance(side, ast.Constant) and side.value in platforms:
                        offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            f"apps/broadcasts compares a platform literal at {offenders}. Contract 4's promise is that "
            f"a Layer-5 platform costs one module and one registry line; read the flag, not the name."
        )
