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
import re
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

    def test_a_template_with_an_unfilled_slot_is_refused(self, tenancy, whatsapp_connection, make_contacts):
        """Meta rejects a template message whose parameter count does not match.

        Checked once, here, rather than discovered once per recipient — and the
        slot list comes from ``slots_for``, the same reading of ``body_structure``
        the composer built its form from.
        """
        from apps.broadcasts.tests.conftest import EVERYONE
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=whatsapp_connection,
            name="two_slots",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hi {{1}}, your order {{2}} shipped."}},
        )
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Shipping", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        with pytest.raises(services.BroadcastError, match="body.2"):
            services.save_template(broadcast, template, {"body.1": "Ada"})

    def test_a_template_from_another_channel_is_refused(self, tenancy, whatsapp_connection, connection):
        """A template name is scoped to the WABA, so a reference picked against
        one number means nothing on another — and can silently mean something
        *else*. ``sendable`` says the same at send time; this is the composer's
        half."""
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name="elsewhere",
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hi"}},
        )
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Wrong number", connection=whatsapp_connection, user=tenancy.owner
        )

        with pytest.raises(services.BroadcastError, match="not approved for use on this channel"):
            services.save_template(broadcast, template, {})

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

    #: Any comparison against a platform name, whatever the operator, whatever the
    #: quoting, and however much whitespace sits around it. Written as one regex
    #: rather than a tuple of literal needles: the tuple this replaced held the
    #: same string twice and matched neither ``platform=="whatsapp"`` nor
    #: ``platform != "sms"``, so the guard it was supposed to be had two holes in
    #: it and one of them was a copy-paste.
    PLATFORM_COMPARISON = re.compile(
        r"""platform\s*[!=]=\s*['"](?:telegram|instagram|messenger|whatsapp|sms|email)['"]"""
    )

    def test_no_template_branches_on_a_platform_name(self):
        offenders = []
        for path in sorted((self.ROOT / "templates" / "broadcasts").glob("*.html")):
            # `platform|platform_class` is a *style* lookup and is fine; a
            # comparison is what decides an affordance.
            for match in self.PLATFORM_COMPARISON.finditer(path.read_text()):
                offenders.append(f"{path.name}: {match.group(0)}")
        assert not offenders, (
            f"A composer template branches on a platform name at {offenders}. Every affordance has to "
            f"come from apps/broadcasts/composer.py, which reads the capability and policy tables — see "
            f"docs/agent-prompts/layer-6.md."
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


@pytest.mark.django_db
class TestNoAudienceYet:
    """A draft with no rules at all — the state the audience step opens in.

    An empty document is the *absence* of a filter, and the condition engine
    cannot compile one: ``conditions.queryset(ws, {})`` raises "missing key(s):
    match, rules". So every path that might meet one has to recognise it, or a
    freshly created broadcast 500s on the first keystroke.

    ``{"match": "all", "rules": []}`` is a different thing and is deliberately
    *not* caught here: it targets the whole workspace, which is the hazard
    apps/contacts/conditions.py names by issue number, and the answer to it is
    the count this preview shows rather than a refusal.
    """

    def test_the_preview_answers_without_asking_the_engine(self, tenancy, client_for, connection):
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        response = client_for(tenancy.owner).get(
            _url("broadcasts:audience_preview", tenancy, broadcast_id=broadcast.pk)
        )

        assert response.status_code == 200
        assert b"Add a rule" in response.content

    def test_the_composer_page_opens_on_a_draft_with_no_audience(self, tenancy, client_for, connection):
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        response = client_for(tenancy.owner).get(_url("broadcasts:compose", tenancy, broadcast_id=broadcast.pk))

        assert response.status_code == 200

    def test_saving_an_empty_audience_is_refused_in_words_an_operator_can_act_on(self, tenancy, client_for, connection):
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )

        response = client_for(tenancy.owner).post(
            _url("broadcasts:save_audience", tenancy, broadcast_id=broadcast.pk), {"filter": ""}
        )

        assert b"Add at least one rule" in response.content
        broadcast.refresh_from_db()
        assert broadcast.target_filter_json == {}

    def test_scheduling_one_is_refused_rather_than_crashing(self, tenancy, connection, make_contacts):
        """Reachable through the API, where nothing walks the wizard's steps."""
        make_contacts(1, connection=connection)
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Fresh", connection=connection, user=tenancy.owner
        )
        services.save_content(broadcast, {"blocks": [{"type": "text", "text": "Hi"}]})

        with pytest.raises(services.BroadcastError, match="Choose who"):
            services.schedule_broadcast(broadcast)


@pytest.mark.django_db
class TestTemplateStaysUsable:
    """A template can stop being sendable between composing and sending.

    ``save_template`` checks approval and the connection when the operator picks
    one, but Meta rejects templates on review and the housekeeping poller writes
    that status back — so the check has to be repeated at the moment somebody
    presses send, or every recipient discovers it separately.
    """

    def _approved(self, tenancy, connection, name="ready"):
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        return WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name=name,
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": "Hello"}},
        )

    def _drafted(self, tenancy, connection, template):
        from apps.broadcasts.tests.conftest import EVERYONE

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Notice", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_template(broadcast, template, {})
        return broadcast

    def test_scheduling_is_refused_once_rather_than_failing_per_recipient(
        self, tenancy, whatsapp_connection, make_contacts
    ):
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        make_contacts(3, connection=whatsapp_connection)
        template = self._approved(tenancy, whatsapp_connection, "withdrawn_before_send")
        broadcast = self._drafted(tenancy, whatsapp_connection, template)

        WhatsAppTemplate.objects.for_workspace(tenancy.workspace).filter(pk=template.pk).update(
            status=WhatsAppTemplateStatus.REJECTED
        )

        with pytest.raises(services.BroadcastError, match="no longer approved"):
            services.schedule_broadcast(broadcast)

        broadcast.refresh_from_db()
        assert broadcast.status == BroadcastStatus.DRAFT

    def test_duplicating_re_checks_the_template_rather_than_copying_the_key(self, tenancy, whatsapp_connection):
        """Assignment would carry a rejected template into a fresh draft with
        none of the checks the composer applies."""
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        template = self._approved(tenancy, whatsapp_connection, "withdrawn_before_copy")
        broadcast = self._drafted(tenancy, whatsapp_connection, template)
        WhatsAppTemplate.objects.for_workspace(tenancy.workspace).filter(pk=template.pk).update(
            status=WhatsAppTemplateStatus.REJECTED
        )

        copy = services.duplicate_broadcast(broadcast, user=tenancy.owner)

        assert copy.whatsapp_template_id is None
        # And the copy is an ordinary draft the operator can fix, not a
        # half-built row that fails at send.
        assert copy.status == BroadcastStatus.DRAFT

    def test_a_still_approved_template_duplicates_normally(self, tenancy, whatsapp_connection):
        template = self._approved(tenancy, whatsapp_connection, "still_fine")
        broadcast = self._drafted(tenancy, whatsapp_connection, template)

        copy = services.duplicate_broadcast(broadcast, user=tenancy.owner)

        assert copy.whatsapp_template_id == template.pk


@pytest.mark.django_db
class TestContentValidatesAgainstItsOwnChannel:
    def test_it_validates_against_the_connection_not_the_workspace(
        self, tenancy, connection, whatsapp_connection, monkeypatch
    ):
        """A broadcast runs on exactly one connection, so that is the platform its
        content should be judged against.

        ``validate_for_workspace`` resolves the set to every platform the
        workspace has connected, which collects capability warnings about
        channels this message will never touch and, on a workspace whose other
        channel is more capable, misses the one it will.
        """
        from apps.broadcasts.tests.conftest import EVERYONE

        seen: list[tuple[str, ...]] = []
        real = services.validate_graph

        def spy(graph, **kwargs):
            seen.append(tuple(kwargs.get("platforms", ())))
            return real(graph, **kwargs)

        monkeypatch.setattr(services, "validate_graph", spy)

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Gallery", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)
        services.save_content(broadcast, {"blocks": [{"type": "text", "text": "Hi"}]})

        assert seen == [(whatsapp_connection.platform,)]
        assert connection.platform not in seen[0]


@pytest.mark.django_db
class TestTemplateVariablesAreExactlyTheSlots:
    """Meta binds template parameters by position, not by name.

    ``apps.channels.providers.whatsapp._template_components`` renders every
    stored slot as a contiguous run, so an extra entry is not inert — it makes
    the parameter count disagree with the template Meta reviewed, and the whole
    message is refused, once per recipient.
    """

    def _template(self, tenancy, connection, name, body):
        from apps.channels.models import WhatsAppTemplate, WhatsAppTemplateStatus

        return WhatsAppTemplate.objects.create(
            workspace=tenancy.workspace,
            channel_connection=connection,
            name=name,
            language="en_US",
            category="utility",
            status=WhatsAppTemplateStatus.APPROVED,
            body_structure={"body": {"text": body}},
        )

    def test_a_slot_the_template_does_not_declare_is_dropped(self, tenancy, whatsapp_connection):
        """The shape the composer produces when an operator switches templates:
        Alpine keeps the first template's entries in its state."""
        from apps.broadcasts.tests.conftest import EVERYONE

        one_slot = self._template(tenancy, whatsapp_connection, "one_slot", "Hi {{1}}.")
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Switch", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        services.save_template(broadcast, one_slot, {"body.1": "Ada", "body.2": "left over", "header.1": "stale"})

        broadcast.refresh_from_db()
        assert broadcast.template_variables == {"body.1": "Ada"}

    def test_a_missing_slot_is_still_refused(self, tenancy, whatsapp_connection):
        from apps.broadcasts.tests.conftest import EVERYONE

        two_slots = self._template(tenancy, whatsapp_connection, "two_slots", "Hi {{1}}, order {{2}}.")
        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Gappy", connection=whatsapp_connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        with pytest.raises(services.BroadcastError, match="body.2"):
            services.save_template(broadcast, two_slots, {"body.1": "Ada", "body.9": "nonsense"})


@pytest.mark.django_db
class TestGalleryIsNotOffered:
    def test_the_composer_does_not_advertise_a_block_it_cannot_build(self, tenancy, connection):
        """The schema's block_gallery requires a `cards` array and this composer
        has no multi-card editor, so the button could only produce a block the
        validator refuses."""
        from apps.channels.capabilities import capabilities_for

        assert "gallery" not in composer.BLOCK_KINDS
        # And the exclusion is the composer's own, not an accident of an empty
        # capability table: some platform does render galleries, and it still
        # does not get the button.
        assert [p for p in Platform.values if capabilities_for(p).gallery], (
            "no platform declares gallery support, so this test proves nothing"
        )
        offered = {
            kind
            for platform in Platform.values
            for kind in composer.BLOCK_KINDS
            if capabilities_for(platform).supports_block(kind)
        }
        assert "gallery" not in offered

    def test_a_hand_crafted_gallery_is_still_refused(self, tenancy, connection):
        """Nothing offers it, and the validator is the backstop if something did."""
        from apps.broadcasts.tests.conftest import EVERYONE

        broadcast = services.create_broadcast(
            workspace=tenancy.workspace, name="Gallery", connection=connection, user=tenancy.owner
        )
        services.set_audience(broadcast, filter_json=EVERYONE)

        with pytest.raises(services.BroadcastError):
            services.save_content(broadcast, {"blocks": [{"type": "gallery", "media_id": "not-cards"}]})
