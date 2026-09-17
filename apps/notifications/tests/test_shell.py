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
    def test_it_renders_in_the_app_header(self, shell_body):
        """The bell moved out of the sidebar footer when the header arrived.

        It is now the only unread indicator in the product — there is no nav
        row and no mobile-bar copy — so it has to be somewhere every page
        renders. The header is, including on settings pages.
        """
        header = shell_body[shell_body.index('class="appbar"') : shell_body.index("</header>")]

        assert reverse("notifications:bell") in header
        assert "appbar-bell" in header

    def test_the_bell_is_the_only_way_in_and_is_not_workspace_scoped(self, shell_body):
        """A notification is addressed to a person, and the feed spans every
        workspace they belong to. Nothing about reaching it may be scoped to a
        workspace — that would make it vanish for a user whose workspaces are
        all archived, which is exactly when a channel alert matters most.
        """
        assert reverse("notifications:bell") == "/notifications/bell/"
        assert reverse("notifications:badge") == "/notifications/badge/"
        # The rail no longer carries a Notifications row.
        assert "notifications" not in [i.key for g in MAIN_NAV for i in g.items]

    def test_the_dropdown_opens_downward_because_the_bell_is_at_the_top(self):
        """It used to open upward: it hung off the bottom of a sidebar. From
        the top-right of a header, upward runs off the top of the window and
        left-aligned runs off the right edge."""
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        assert "top-full" in markup
        assert "bottom-full" not in markup


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

    @pytest.mark.parametrize("element_id", ["notification-badge"])
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

    @pytest.mark.parametrize("unread", [0, 1])
    def test_the_response_fragment_updates_every_place_the_count_appears(self, tenancy, client_for, unread):
        """One, where there used to be three.

        The count appeared in the sidebar footer's bell, the mobile top bar and
        the nav's Notifications row, and leaving any of them stale after "mark
        all read" showed one indicator contradicting another. The redesign
        removed two of those surfaces outright — the mobile bar became the
        rail's bottom tab strip, and Notifications left the nav for the header
        bell — so there is one surface and one id.

        This count and _badge.html have to change together: a target removed
        from one side and not the other is htmx:oobErrorNoTarget once a minute
        for the whole session, with no test failure and no visible symptom.

        Both counts, because the partial has a branch per count and the zero one
        is the branch that regressed before: it used to drop the target entirely.
        """
        for i in range(unread):
            Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title=f"Ping {i}")

        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'id="notification-badge"' in body
        assert body.count("hx-swap-oob") == 1


@pytest.mark.django_db
class TestThePolledBadgeAlwaysHasATarget:
    """The 60s poll in _bell.html fires on every authenticated page, and an
    out-of-band swap with no element to land on is an error htmx logs and
    nothing else notices. Both ends of that swap are asserted here: the shell
    renders the target, and the response keeps it.
    """

    def test_the_shell_renders_the_slot_with_nothing_unread(self, shell_body):
        assert shell_body.count('id="notification-badge"') == 1

    def test_the_slot_shows_no_dot_and_no_zero(self, shell_body):
        """Present in the document, invisible on the screen.

        Scoped to the slot element rather than to the page: `notif-dot` as a
        bare substring would still match a different surface, failing this test
        for something it is not about.
        """
        slot = re.search(r'<span id="notification-badge"[^>]*>(.*?)</span>', shell_body)
        assert slot, "the poll has no target"
        assert slot.group(1) == ""
        assert "notif-dot" not in slot.group(0)

    def test_reaching_zero_empties_the_slot_rather_than_deleting_it(self, tenancy, client_for):
        """Deleting it was the original bug: an element the response removes is
        one every later poll cannot find, and the count could then never come
        back up in the page either, because every subsequent swap was aiming at
        something that was gone."""
        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'hx-swap-oob="delete"' not in body
        assert 'id="notification-badge"' in body
        assert "notif-dot" not in body

    def test_every_target_the_response_swaps_is_one_the_shell_renders(self, tenancy, client_for):
        """Generalised twice over: across the response's targets, so a fourth
        surface cannot be added to _badge.html without being added to the
        shell, and across the shell's *layouts*, because they are not
        interchangeable. layouts/settings.html replaces {% block sidebar_nav %}
        wholesale while base.html keeps rendering the poll outside it, so a
        settings page carried the bell and none of the nav's badge ids — the
        error this whole class is about, on 14 pages, missed by sampling only
        the dashboard.
        """
        client = client_for(tenancy.owner)
        response = client.get(reverse("notifications:badge")).content.decode()
        targets = re.findall(r'id="([^"]+)"[^>]*hx-swap-oob', response)
        assert targets

        for url in self.page_archetypes(tenancy):
            shell = client.get(url).content.decode()
            for target in targets:
                assert f'id="{target}"' in shell, f"{target} missing from {url}"

    def test_no_archetype_carries_a_target_twice(self, tenancy, client_for):
        """The other half: a sink that duplicates a slot the nav already
        rendered would send every swap to whichever came first."""
        client = client_for(tenancy.owner)
        response = client.get(reverse("notifications:badge")).content.decode()
        targets = re.findall(r'id="([^"]+)"[^>]*hx-swap-oob', response)

        for url in self.page_archetypes(tenancy):
            shell = client.get(url).content.decode()
            for target in targets:
                assert shell.count(f'id="{target}"') == 1, f"{target} in {url}"

    def test_the_settings_archetype_carries_the_target_like_any_other(self, tenancy, client_for):
        """What the sinks used to work around, now true by construction.

        A settings page used to replace the whole nav, taking the badge ids with
        it while base.html went on rendering the poll — so
        partials/_nav_badge_sinks.html existed to put hidden copies back. The
        settings nav is a second column beside the rail now, the header renders
        there too, and the bell's target is simply present the way it is
        everywhere else.
        """
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get("/accounts/settings/").content.decode()

        assert body.count('id="notification-badge"') == 1
        assert "appbar-bell" in body

    @staticmethod
    def page_archetypes(tenancy):
        """One URL per sidebar_nav layout in the product. The guard test below
        fails if a new layout appears and this list does not grow."""
        return [
            f"/w/{tenancy.workspace.id}/",
            "/accounts/settings/",
            reverse("workspaces:settings", kwargs={"workspace_id": tenancy.workspace.id}),
        ]

    def test_no_layout_replaces_the_navigation_any_more(self):
        """The guard that took the sinks' place.

        partials/_nav_badge_sinks.html existed because a layout could swap the
        whole nav out and strand the badge ids the bell's poll aims at. Nothing
        does that now — the settings nav is a second column — so the sinks were
        deleted. Discovery rather than a hand-kept list: a template that brings
        the override back has to bring the sinks back with it, and should fail
        here rather than in a console nobody reads.
        """
        templates = BASE_HTML.parent
        offenders = []
        for path in sorted(templates.rglob("*.html")):
            if path == BASE_HTML:
                continue
            # Comments stripped first: several templates *describe* the old
            # override in prose, and matching prose would fire on files that
            # render no nav at all.
            markup = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", path.read_text(), flags=re.S)
            if re.search(r"{%\s*block sidebar_nav\s*%}", markup):
                offenders.append(path.relative_to(templates))

        assert not offenders, (
            "replaces the navigation wholesale, which strands the bell's out-of-band badge targets; "
            f"restore partials/_nav_badge_sinks.html with it: {offenders}"
        )


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

    def test_the_count_the_bell_renders_is_the_one_the_context_names(self, tenancy):
        """There is no nav row to compare against any more — the bell's dot is
        the only surface — so the agreement asserted here is between the named
        context key and what the shell actually renders from it."""
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Mine")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 1
        # And no rail row claims the count, which would be a second thing to
        # keep in step with it.
        assert not [i for g in context["nav_groups"] for i in g["items"] if i["key"] == "notifications"]


def _request_for(user, workspace):
    from django.test import RequestFactory

    request = RequestFactory().get(f"/w/{workspace.id}/")
    request.user = user
    request.workspace = workspace
    request.org_membership = None
    return request
