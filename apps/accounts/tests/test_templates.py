"""The auth pages render, on their own, before issue #32 ships the shell."""

import pytest

from tests.support import create_user

ANONYMOUS_PAGES = [
    "/accounts/login/",
    "/accounts/signup/",
    "/accounts/password/reset/",
]

AUTHENTICATED_PAGES = [
    "/accounts/logout/",
    "/accounts/password/change/",
    "/accounts/email/",
    "/accounts/settings/",
]


@pytest.mark.django_db
class TestAuthPagesRender:
    @pytest.mark.parametrize("path", ANONYMOUS_PAGES)
    def test_anonymous_pages(self, client, path):
        response = client.get(path)

        assert response.status_code == 200
        assert b"BrightBean Chat" in response.content

    @pytest.mark.parametrize("path", AUTHENTICATED_PAGES)
    def test_authenticated_pages(self, client_for, tenancy, path):
        """allauth's own templates go through the one layout override, so they
        land inside the shell rather than allauth's default HTML document."""
        response = client_for(tenancy.owner).get(path, follow=True)

        assert response.status_code == 200
        assert b"BrightBean Chat" in response.content

    def test_the_password_reset_confirmation_renders(self, client):
        create_user("resetme@example.test")

        response = client.post("/accounts/password/reset/", {"email": "resetme@example.test"}, follow=True)

        assert response.status_code == 200

    def test_signing_out_works(self, client_for, tenancy):
        response = client_for(tenancy.owner).post("/accounts/logout/", follow=True)

        assert not response.wsgi_request.user.is_authenticated


@pytest.mark.django_db
class TestAppPagesRender:
    def test_the_dashboard(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/")

        assert response.status_code == 200
        assert tenancy.workspace.name.encode() in response.content

    @pytest.mark.parametrize(
        "path",
        [
            "/organization/settings/",
            "/organization/workspaces/",
            "/organization/members/",
        ],
    )
    def test_org_pages(self, tenancy, client_for, path):
        assert client_for(tenancy.owner).get(path).status_code == 200

    def test_workspace_settings_and_credentials(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        assert client.get(f"/w/{tenancy.workspace.pk}/settings/").status_code == 200
        assert client.get(f"/w/{tenancy.workspace.pk}/settings/credentials/").status_code == 200
        assert client.get(f"/w/{tenancy.workspace.pk}/settings/credentials/instagram/").status_code == 200

    def test_the_member_workspace_access_form(self, tenancy, client_for):
        from apps.members.models import OrgMembership

        membership = OrgMembership.objects.get(user=tenancy.user_for("agent"))

        response = client_for(tenancy.owner).get(f"/organization/members/{membership.pk}/workspaces/")

        assert response.status_code == 200
        assert b"Save access" in response.content


@pytest.mark.django_db
class TestTheShellHandoverIsComplete:
    """Issue #31 shipped a placeholder `base.html` under the app-templates
    directory so its pages rendered while both branches were open, on the
    understanding that #32's project-level `templates/base.html` would take
    over and the placeholder would be deleted. This is that handover."""

    def test_the_real_shell_is_the_one_being_used(self):
        from pathlib import Path

        from django.conf import settings
        from django.template.loader import get_template

        origin = Path(get_template("base.html").origin.name)

        assert origin == Path(settings.BASE_DIR) / "templates" / "base.html"

    def test_the_placeholder_is_gone(self):
        """Leaving it would be a second shell that silently shadows nothing —
        until someone reorders TEMPLATES["DIRS"] and it shadows everything."""
        from pathlib import Path

        from django.conf import settings

        assert not (Path(settings.BASE_DIR) / "apps" / "common" / "templates" / "base.html").exists()

    def test_the_auth_pages_render_inside_the_real_shell(self, client):
        """The block contract #31 wrote against is the one #32 shipped."""
        body = client.get("/accounts/login/").content.decode()

        assert "auth-card" in body
        assert "css/dist/styles.css" in body

    def test_inline_scripts_carry_the_csp_nonce(self, client):
        """SECURITY-BASELINE §8, and the pattern #32 keeps for the real shell."""
        response = client.get("/accounts/login/")

        assert b'<script nonce="' in response.content


@pytest.mark.django_db
class TestSignupUsesTheInviteAwareView:
    def test_the_local_route_shadows_allauths(self):
        """config/urls.py mounts apps.accounts.urls before allauth.urls; get it
        the wrong way round and invite prefill silently stops happening."""
        from django.urls import resolve

        from apps.accounts.views_signup import InvitePrefillSignupView

        match = resolve("/accounts/signup/")

        assert match.func.view_class is InvitePrefillSignupView
