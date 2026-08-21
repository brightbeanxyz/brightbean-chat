"""The scaffold's two views: the placeholder page and /healthz."""

from unittest.mock import patch

import pytest
from django.db.utils import OperationalError


@pytest.mark.django_db
class TestHealthz:
    def test_returns_ok_when_the_database_answers(self, client):
        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok"}

    def test_returns_503_when_the_database_is_down(self, client):
        with patch("apps.common.views.connections") as connections:
            connections.__getitem__.return_value.cursor.side_effect = OperationalError(
                "could not connect to server: host=db-primary.internal port=5432"
            )

            response = client.get("/healthz")

        assert response.status_code == 503
        assert response.json() == {"status": "error", "database": "unavailable"}

    def test_failure_body_leaks_no_connection_detail(self, client):
        with patch("apps.common.views.connections") as connections:
            connections.__getitem__.return_value.cursor.side_effect = OperationalError(
                "could not connect to server: host=db-primary.internal port=5432"
            )

            response = client.get("/healthz")

        body = response.content.decode()
        assert "db-primary.internal" not in body
        assert "5432" not in body

    def test_is_exempt_from_the_production_https_redirect(self):
        """A plain-HTTP probe inside the network must not get a 301."""
        import re

        from config.settings import production

        assert any(re.match(pattern, "healthz") for pattern in production.SECURE_REDIRECT_EXEMPT)


@pytest.mark.django_db
class TestRoot:
    """Layer 0's placeholder page is gone; "/" routes people now (issue #31)."""

    def test_anonymous_is_sent_to_the_login_page(self, client):
        response = client.get("/")

        assert response.status_code == 302
        assert response.headers["Location"] == "/accounts/login/"

    def test_the_login_page_renders(self, client):
        response = client.get("/accounts/login/")

        assert response.status_code == 200
        assert b"BrightBean Chat" in response.content


@pytest.mark.django_db
class TestErrorTemplates:
    def test_404_uses_the_project_template(self, client):
        response = client.get("/no-such-page")

        assert response.status_code == 404
        assert b"404 Not Found" in response.content

    @pytest.mark.parametrize("name", ["403.html", "404.html", "500.html"])
    def test_error_templates_exist(self, name):
        """Studio ships none of these; #32 restyles them."""
        from django.template.loader import get_template

        assert get_template(name) is not None
