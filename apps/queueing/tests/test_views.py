"""``/internal/tick`` — the token gate and the drain behind it."""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.queueing.models import ActionStatus, ScheduledAction
from apps.queueing.tests.support import make_action, temporary_handler
from tests.support import Tenancy

TOKEN = "tick-token-for-tests"
PROBE = "view_probe"


def _noop(payload: dict[str, Any], action: ScheduledAction) -> None:
    return None


@pytest.fixture
def url() -> str:
    return reverse("internal_tick")


@pytest.mark.django_db
class TestTokenGate:
    def test_404s_when_no_token_is_configured(self, client: Client, url: str, settings: Any) -> None:
        """An unconfigured deployment must not advertise that the route exists."""
        settings.TICK_TOKEN = ""
        assert client.get(url).status_code == 404
        assert client.get(f"{url}?token=anything").status_code == 404

    def test_404s_on_a_wrong_token(self, client: Client, url: str, settings: Any) -> None:
        settings.TICK_TOKEN = TOKEN
        response = client.get(f"{url}?token=wrong")

        assert response.status_code == 404
        # Bare 404, no hint about what was wrong (SECURITY-BASELINE §4).
        assert TOKEN not in response.content.decode()

    def test_404s_on_a_missing_token(self, client: Client, url: str, settings: Any) -> None:
        settings.TICK_TOKEN = TOKEN
        assert client.get(url).status_code == 404

    def test_a_prefix_of_the_token_is_not_enough(self, client: Client, url: str, settings: Any) -> None:
        settings.TICK_TOKEN = TOKEN
        assert client.get(f"{url}?token={TOKEN[:-1]}").status_code == 404

    def test_whitespace_around_a_configured_token_is_ignored(self, client: Client, url: str, settings: Any) -> None:
        """A trailing newline in an env var should not silently disable the route."""
        settings.TICK_TOKEN = f"  {TOKEN}\n"
        assert client.get(f"{url}?token={TOKEN}").status_code == 200

    def test_the_comparison_is_constant_time(self, client: Client, url: str, settings: Any, monkeypatch: Any) -> None:
        """Pin the helper, not a timing measurement — a timing test is a flake generator."""
        from apps.queueing import views

        calls: list[tuple[str, str]] = []
        real = views.constant_time_compare

        def spy(left: str, right: str) -> bool:
            calls.append((left, right))
            return real(left, right)

        monkeypatch.setattr(views, "constant_time_compare", spy)
        settings.TICK_TOKEN = TOKEN
        client.get(f"{url}?token=wrong")

        assert calls == [("wrong", TOKEN)]

    @pytest.mark.parametrize("method", ["delete", "put", "patch", "head", "options"])
    def test_an_unauthenticated_caller_learns_nothing_from_the_method(
        self, client: Client, url: str, settings: Any, method: str
    ) -> None:
        """A 405 here would be a route-existence oracle.

        The method check has to run *after* the token check, or an
        unauthenticated HEAD answers 405 with an Allow header while every
        unmounted path answers 404 — which tells a caller holding no token that
        this route exists, and so that the deployment runs this queue.
        Same reasoning as CONTRIBUTING.md's rule for stacking @require_POST
        innermost on the tenant views.
        """
        settings.TICK_TOKEN = TOKEN
        bad_token = getattr(client, method)(f"{url}?token=wrong")
        no_token = getattr(client, method)(url)

        assert bad_token.status_code == 404
        assert no_token.status_code == 404
        assert "Allow" not in bad_token

    @pytest.mark.parametrize("method", ["get", "post", "delete", "head"])
    def test_an_unconfigured_deployment_answers_404_whatever_the_method(
        self, client: Client, url: str, settings: Any, method: str
    ) -> None:
        settings.TICK_TOKEN = ""
        assert getattr(client, method)(url).status_code == 404

    def test_the_token_never_reaches_the_logs(self, client: Client, url: str, settings: Any, caplog: Any) -> None:
        """It rides in a query string, and request paths are logged (SECURITY-BASELINE §5)."""
        settings.TICK_TOKEN = TOKEN
        with caplog.at_level("DEBUG"):
            client.get(f"{url}?token={TOKEN}")

        assert TOKEN not in caplog.text


@pytest.mark.django_db
class TestDrain:
    def test_a_good_token_drains_the_queue(self, client: Client, url: str, settings: Any, tenancy: Tenancy) -> None:
        settings.TICK_TOKEN = TOKEN
        make_action(tenancy.workspace, type=PROBE)
        make_action(tenancy.workspace, type=PROBE)

        with temporary_handler(PROBE, _noop):
            response = client.get(f"{url}?token={TOKEN}")

        assert response.status_code == 200
        body = response.json()
        assert body["claimed"] == 2
        assert body["done"] == 2
        assert body["failed"] == 0
        assert "duration_ms" in body
        assert ScheduledAction.objects.for_workspace(tenancy.workspace).filter(status=ActionStatus.DONE).count() == 2

    def test_post_works_too(self, client: Client, url: str, settings: Any) -> None:
        """External pingers use both verbs, and POST carries no CSRF cookie."""
        settings.TICK_TOKEN = TOKEN
        assert client.post(f"{url}?token={TOKEN}").status_code == 200

    def test_other_methods_are_refused_once_the_caller_is_proven(self, client: Client, url: str, settings: Any) -> None:
        settings.TICK_TOKEN = TOKEN
        response = client.delete(f"{url}?token={TOKEN}")

        assert response.status_code == 405
        assert response["Allow"] == "GET, POST"

    def test_it_bootstraps_the_housekeeping_chain(self, client: Client, url: str, settings: Any) -> None:
        """A cron-only host never runs the worker, so the tick has to do this."""
        settings.TICK_TOKEN = TOKEN
        client.get(f"{url}?token={TOKEN}")

        assert ScheduledAction.objects.unscoped().filter(type="housekeeping").exists()

    def test_no_authentication_is_required_beyond_the_token(self, client: Client, url: str, settings: Any) -> None:
        """The caller is a cron service, not a signed-in user."""
        settings.TICK_TOKEN = TOKEN
        response = client.get(f"{url}?token={TOKEN}")

        assert response.status_code == 200
        assert "login" not in response["Content-Type"]
