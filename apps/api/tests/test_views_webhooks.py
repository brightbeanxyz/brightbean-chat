"""The workspace webhooks settings page.

Workspace-tier, gated on ``manage_workspace_settings`` — ``outbound_webhook``
carries a ``workspace_id`` in SPEC §5, so its url, secret and subscriptions
belong to one workspace's data rather than to the organization.
"""

import pytest
from django.db import connection

from apps.api.models import OutboundWebhook, WebhookDelivery
from apps.api.tests.support import FakeInternet, serving
from apps.common.outbound import reset_deployment_cache
from apps.members.roles import WorkspaceRole


@pytest.fixture(autouse=True)
def _clear_deployment_cache():
    reset_deployment_cache()
    yield
    reset_deployment_cache()


def base(tenancy):
    return f"/w/{tenancy.workspace.pk}/settings/webhooks/"


@pytest.mark.django_db
class TestPageAccess:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (WorkspaceRole.ADMIN, 200),
            (WorkspaceRole.EDITOR, 403),
            (WorkspaceRole.AGENT, 403),
            (WorkspaceRole.VIEWER, 403),
        ],
    )
    def test_only_a_workspace_admin_reaches_it(self, client_for, tenancy, role, expected):
        response = client_for(tenancy.user_for(role)).get(base(tenancy))

        assert response.status_code == expected

    def test_another_workspaces_page_is_a_404(self, client_for, tenancy, other_tenancy):
        response = client_for(tenancy.owner).get(base(other_tenancy))

        assert response.status_code == 404


@pytest.mark.django_db
class TestCreating:
    def test_it_creates_and_shows_the_secret_once(self, client_for, tenancy):
        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/",
            {"url": "https://receiver.example.com/hooks", "events": ["contact.created"]},
        )

        assert response.status_code == 200
        webhook = OutboundWebhook.objects.for_workspace(tenancy.workspace).get()
        assert webhook.events == ["contact.created"]
        assert webhook.enabled is True
        assert webhook.secret in response.content.decode()
        # Stored encrypted, so a database dump does not hand over the ability to
        # forge a signature for this endpoint.
        with connection.cursor() as cursor:
            cursor.execute("SELECT secret FROM api_outbound_webhook WHERE id = %s", [str(webhook.pk)])
            stored = cursor.fetchone()[0]
        assert webhook.secret not in stored

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "ftp://receiver.example.com/hooks",
            "https://user:pass@receiver.example.com/hooks",
            "not a url",
        ],
    )
    def test_a_url_that_is_wrong_on_its_face_is_refused(self, client_for, tenancy, url):
        """Syntactic checks only — the address itself is the guard's business.

        Resolving here would be a check with a shelf life: DNS can point
        somewhere else by the time a delivery goes out, which is the rebind the
        guard pins against.
        """
        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/", {"url": url, "events": ["contact.created"]}
        )

        assert response.status_code == 400
        assert not OutboundWebhook.objects.for_workspace(tenancy.workspace).exists()

    def test_a_private_url_is_accepted_here_and_refused_at_delivery(self, client_for, tenancy):
        """The division of labour, made explicit.

        ``http://127.0.0.1/hooks`` is a syntactically fine URL. It is the guard,
        at delivery time, that refuses it — and with
        ``EXTERNAL_REQUEST_ALLOW_PRIVATE`` on, an on-prem deployment can use it.
        """
        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/", {"url": "http://127.0.0.1:9000/hooks", "events": ["contact.created"]}
        )

        assert response.status_code == 200

    def test_a_workspace_cannot_hoard_endpoints(self, client_for, tenancy):
        from apps.api.services import MAX_WEBHOOKS_PER_WORKSPACE, create_webhook

        for index in range(MAX_WEBHOOKS_PER_WORKSPACE):
            create_webhook(
                workspace=tenancy.workspace,
                url=f"https://receiver{index}.example.com/hooks",
                events=["contact.created"],
            )

        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/",
            {"url": "https://one-too-many.example.com/hooks", "events": ["contact.created"]},
        )

        assert response.status_code == 400
        assert OutboundWebhook.objects.for_workspace(tenancy.workspace).count() == MAX_WEBHOOKS_PER_WORKSPACE

    def test_an_unknown_event_is_refused(self, client_for, tenancy):
        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/",
            {"url": "https://receiver.example.com/hooks", "events": ["contact.exploded"]},
        )

        assert response.status_code == 400

    def test_no_events_is_refused(self, client_for, tenancy):
        response = client_for(tenancy.owner).post(
            base(tenancy) + "create/", {"url": "https://receiver.example.com/hooks"}
        )

        assert response.status_code == 400


@pytest.mark.django_db
class TestEditing:
    def test_re_enabling_clears_the_failure_streak(self, client_for, tenancy, webhook, settings):
        """Otherwise it switches itself off again on the next failure.

        Which reads, from the operator's side, as "re-enabling does not work".
        """
        webhook.enabled = False
        webhook.consecutive_failures = settings.API_WEBHOOK_MAX_CONSECUTIVE_FAILURES
        webhook.save(update_fields=["enabled", "consecutive_failures"])

        client_for(tenancy.owner).post(
            f"{base(tenancy)}{webhook.pk}/update/",
            {"url": webhook.url, "events": ["contact.created"], "enabled": "on"},
        )

        webhook.refresh_from_db()
        assert webhook.enabled is True
        assert webhook.consecutive_failures == 0
        assert webhook.disabled_at is None

    def test_rotating_shows_a_new_secret_once(self, client_for, tenancy, webhook):
        old = webhook.secret

        response = client_for(tenancy.owner).post(f"{base(tenancy)}{webhook.pk}/rotate-secret/")

        webhook.refresh_from_db()
        assert webhook.secret != old
        assert webhook.secret in response.content.decode()

    def test_deleting_takes_the_delivery_log_with_it(self, client_for, tenancy, webhook):
        WebhookDelivery.objects.create(
            workspace=tenancy.workspace, webhook=webhook, event="contact.created", status="succeeded"
        )

        client_for(tenancy.owner).post(f"{base(tenancy)}{webhook.pk}/delete/")

        assert not OutboundWebhook.objects.for_workspace(tenancy.workspace).exists()
        assert not WebhookDelivery.objects.for_workspace(tenancy.workspace).exists()

    def test_another_workspaces_endpoint_is_a_404(self, client_for, tenancy, other_tenancy):
        theirs = OutboundWebhook(
            workspace=other_tenancy.workspace, url="https://theirs.example.com/h", events=["contact.created"]
        )
        theirs.rotate_secret()
        theirs.save()

        response = client_for(tenancy.owner).get(f"{base(tenancy)}{theirs.pk}/")

        assert response.status_code == 404


@pytest.mark.django_db
class TestTestButton:
    def test_it_delivers_and_reports(self, client_for, tenancy, webhook, monkeypatch):
        internet = FakeInternet(serving(200)).install(monkeypatch)

        response = client_for(tenancy.owner).post(f"{base(tenancy)}{webhook.pk}/test/", follow=True)

        assert len(internet.requests) == 1
        assert "Test delivered" in response.content.decode()
        assert WebhookDelivery.objects.for_workspace(tenancy.workspace).get().event == "webhook.test"

    def test_it_uses_the_shorter_web_tier_deadline(self, client_for, tenancy, webhook, monkeypatch, settings):
        """This is the one delivery that occupies a request thread.

        The worker's ten seconds is the wrong budget for something holding a
        gunicorn thread, so the test path has its own — without it a handful of
        operators testing dead endpoints could hold every thread the app has.
        """
        settings.API_WEBHOOK_TEST_TIMEOUT_SECONDS = 3
        settings.API_WEBHOOK_TIMEOUT_SECONDS = 30
        seen: list[float] = []

        from apps.api import delivery as delivery_module

        real = delivery_module.guarded_request

        def record(method, url, **kwargs):
            seen.append(kwargs["timeout"])
            return real(method, url, **kwargs)

        monkeypatch.setattr(delivery_module, "guarded_request", record)
        FakeInternet(serving(200)).install(monkeypatch)

        client_for(tenancy.owner).post(f"{base(tenancy)}{webhook.pk}/test/")

        assert seen == [3]

    def test_a_failure_is_reported_rather_than_swallowed(self, client_for, tenancy, webhook, monkeypatch):
        FakeInternet(serving(500)).install(monkeypatch)

        response = client_for(tenancy.owner).post(f"{base(tenancy)}{webhook.pk}/test/", follow=True)

        assert "Test failed" in response.content.decode()


@pytest.mark.django_db
class TestDetailPage:
    def test_it_shows_the_recent_deliveries(self, client_for, tenancy, webhook):
        WebhookDelivery.objects.create(
            workspace=tenancy.workspace,
            webhook=webhook,
            event="contact.created",
            status="succeeded",
            response_code=204,
            duration_ms=37,
        )

        body = client_for(tenancy.owner).get(f"{base(tenancy)}{webhook.pk}/").content.decode()

        assert "contact.created" in body
        assert "204" in body
        assert "37 ms" in body

    def test_it_never_renders_the_secret(self, client_for, tenancy, webhook):
        """CONTRIBUTING: never render a stored secret. Rotation is the way back."""
        body = client_for(tenancy.owner).get(f"{base(tenancy)}{webhook.pk}/").content.decode()

        assert webhook.secret not in body
