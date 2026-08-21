"""The bell's contract with the shared shell.

Mirrors the conventions in apps/common/tests/test_shell.py: the shell has
structural invariants that no assertion about a page's *text* would catch, and
issue #7 edits templates/base.html, so it has to keep them.
"""

import re
from pathlib import Path

import pytest
from django.urls import reverse

from apps.common.context_processors import MAIN_NAV, navigation_context
from apps.notifications.models import Notification

BASE_HTML = Path(__file__).resolve().parents[3] / "templates" / "base.html"


@pytest.fixture
def shell_body(tenancy, client_for):
    return client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/").content.decode()


@pytest.mark.django_db
class TestTheBellInTheShell:
    def test_it_renders_in_the_sidebar_footer(self, shell_body):
        footer = shell_body[shell_body.index("sidebar-org-footer") :]

        assert reverse("notifications:bell") in footer

    def test_the_nav_row_points_at_the_history_page(self, shell_body):
        assert 'href="/notifications/"' in shell_body

    def test_the_nav_row_is_not_workspace_scoped(self):
        """A notification is addressed to a person. Scoping the row to a
        workspace would make it vanish for a user whose workspaces are all
        archived — which is exactly when a channel alert matters most."""
        item = next(i for group in MAIN_NAV for i in group.items if i.key == "notifications")

        assert item.workspace_scoped is False

    def test_the_dropdown_opens_upward_because_the_footer_is_at_the_bottom(self):
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        assert "bottom-full" in markup


@pytest.mark.django_db
class TestCspAndAlpineDiscipline:
    """The three traps apps/common/tests/test_shell.py exists to catch, checked
    again on a page that now carries the bell."""

    def test_every_inline_script_still_carries_a_nonce(self, shell_body):
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", shell_body)

        assert scripts
        for tag in scripts:
            assert "nonce=" in tag, tag

    def test_the_bell_adds_no_inline_script_at_all(self):
        """`x-data` on a wrapper is the workspace switcher's pattern, so there
        is no Alpine.data() to register and nothing to slot into base.html's
        load-bearing htmx -> components -> Alpine script order."""
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        assert "<script" not in markup

    def test_no_inline_event_handler_attributes(self, shell_body):
        offenders = re.findall(r"\son(?:click|change|submit|load|error)\s*=", shell_body)

        assert not offenders

    def test_no_element_combines_x_show_with_an_unconditional_hidden(self):
        """Alpine shows an element by clearing its inline display, after which
        a `hidden` utility's own display:none reasserts itself forever."""
        for path in [BASE_HTML, BASE_HTML.parent / "notifications" / "partials" / "_bell.html"]:
            for tag in re.findall(r"<[a-z]+\s[^>]*x-show=[^>]*>", path.read_text(), re.S):
                classes = re.search(r'class="([^"]*)"', tag)
                assert not (classes and "hidden" in classes.group(1).split()), tag

    def test_no_anchor_the_bell_adds_has_an_empty_href(self, tenancy, client_for):
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert 'href=""' not in body


@pytest.mark.django_db
class TestNoDuplicateIds:
    """An out-of-band swap resolves its target by id, so a duplicate does not
    just fail validation — it makes the swap land somewhere arbitrary.

    This caught a real one: the bell's first render included the *response*
    fragment, which carries the nav row's badge id as well as its own, so every
    shell page shipped two `nav-badge-notifications` elements.
    """

    @pytest.mark.parametrize("element_id", ["notification-badge", "nav-badge-notifications"])
    def test_the_shell_carries_each_badge_id_at_most_once(self, tenancy, client_for, element_id):
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/").content.decode()

        assert body.count(f'id="{element_id}"') == 1

    def test_the_first_render_carries_no_out_of_band_attributes(self, tenancy, client_for):
        """hx-swap-oob is meaningful only in a response. In a first render it is
        inert clutter, and its presence means the wrong partial was included."""
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/").content.decode()

        assert "hx-swap-oob" not in body

    def test_the_response_fragment_updates_both_places_the_count_appears(self, tenancy, client_for):
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'id="notification-badge"' in body
        assert 'id="nav-badge-notifications"' in body
        assert body.count("hx-swap-oob") == 2

    def test_reaching_zero_deletes_the_nav_badge_rather_than_emptying_it(self, tenancy, client_for):
        """The shell renders no badge at all for a zero count, so there is
        nothing to swap an empty one into."""
        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'hx-swap-oob="delete"' in body
        assert "notif-dot" not in body


@pytest.mark.django_db
class TestTheAnonymousShell:
    """`/ui/` calls navigation_context directly for requests that never had a
    session, and its docstring promises it "reads no database and no session".
    The badge count and the bell both have to stay out of its way."""

    def test_the_style_guide_still_renders_for_a_visitor_with_no_session(self, client):
        response = client.get("/ui/")

        assert response.status_code == 200

    def test_the_style_guide_shows_no_bell(self, client):
        body = client.get("/ui/").content.decode()

        assert reverse("notifications:bell") not in body

    def test_the_anonymous_path_runs_no_notification_query(self, client, django_assert_num_queries):
        with django_assert_num_queries(0):
            client.get("/ui/")

    def test_a_request_with_no_user_attribute_does_not_raise(self, rf):
        context = navigation_context(rf.get("/"))

        assert context["unread_notification_count"] == 0


@pytest.mark.django_db
class TestTheBadgeCount:
    def test_it_is_zero_and_renders_nothing_when_nothing_is_unread(self, tenancy, shell_body):
        assert "notif-dot" not in shell_body

    def test_it_reflects_only_this_users_unread_rows(self, tenancy, client_for):
        Notification.objects.create(user=tenancy.members["admin"], event_type="inbox_reminder", title="Theirs")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 0

        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Mine")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 1

    def test_the_nav_badge_and_the_named_count_agree(self, tenancy):
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Mine")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        row = next(i for g in context["nav_groups"] for i in g["items"] if i["key"] == "notifications")
        assert row["badge"] == context["unread_notification_count"] == 1


def _request_for(user, workspace):
    from django.test import RequestFactory

    request = RequestFactory().get(f"/w/{workspace.id}/")
    request.user = user
    request.workspace = workspace
    request.org_membership = None
    return request
