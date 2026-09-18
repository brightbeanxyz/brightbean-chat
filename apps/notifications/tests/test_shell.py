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
    def test_it_renders_as_a_row_in_the_sidebar(self, shell_body):
        """The bell moved to the header when the header arrived, and came back
        when it was deleted.

        The requirement never changed: it is the only unread indicator in the
        product — no second copy, no mobile-bar duplicate — so it has to be on
        the product's own nav, which is every page with the product's nav on
        it. Settings is the exception and pays for itself: the rows there are
        the settings rows, and the poll leaves with the row rather than aiming
        at an id the page no longer has. A nav row can carry the count and
        still open the panel.
        """
        sidebar = shell_body[shell_body.index("<aside") : shell_body.index("</aside>")]

        assert reverse("notifications:bell") in sidebar
        assert "sidebar-nav-item" in sidebar
        assert ">Notifications</span>" in sidebar

    def test_the_bell_is_the_only_way_in_and_is_not_workspace_scoped(self, shell_body):
        """A notification is addressed to a person, and the feed spans every
        workspace they belong to. Nothing about reaching it may be scoped to a
        workspace — that would make it vanish for a user whose workspaces are
        all archived, which is exactly when a channel alert matters most.
        """
        assert reverse("notifications:bell") == "/notifications/bell/"
        assert reverse("notifications:badge") == "/notifications/badge/"
        # The row exists, and is the one main-nav row that is not scoped to a
        # workspace — which is the claim this test's name has always made and
        # can now check directly.
        row = next(i for g in MAIN_NAV for i in g.items if i.key == "notifications")
        assert row.workspace_scoped is False

    def test_the_panel_escapes_the_nav_s_scroll_container(self):
        """It has hung off a sidebar footer and off a header's top-right in
        turn; from a row inside the nav neither works, and not for a reason a
        utility can fix. `.sidebar-nav` is `overflow-y: auto`, so it clips an
        absolutely positioned child escaping sideways, and `overflow-x:
        visible` cannot be expressed alongside it. Fixed positioning leaves the
        clipping context at the cost of measuring the trigger on open — the
        same trade templates/components/ui_select.html already makes.

        The clamp is the part worth pinning: without it the 320px panel opens
        off the right edge of a 375px phone, where the drawer puts the trigger
        at x=240.
        """
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        assert "getBoundingClientRect()" in markup
        assert "window.innerWidth" in markup
        assert "fixed" in markup
        # Neither anchoring survives: both are relative to a clipped parent.
        assert "top-full" not in markup
        assert "bottom-full" not in markup

    def test_both_anchor_clamps_are_two_sided(self):
        """`Math.min` alone puts the panel off-screen on a small viewport.

        A landscape phone is 375px tall, so `Math.min(r.top, innerHeight - 420)`
        computed -45 and the panel's header — which carries "Mark all read" —
        sat above the top of the window. A fixed element cannot be scrolled to,
        so the control was simply gone. The same shape applies horizontally
        below 328px wide. Each clamp needs a floor as well as a ceiling.
        """
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        clamps = re.findall(r"this\.(?:top|left)\s*=\s*([^;]+);", markup)

        assert len(clamps) == 2, f"expected a clamp for each axis, got {clamps}"
        for clamp in clamps:
            assert "Math.max(" in clamp, clamp
            assert "Math.min(" in clamp, clamp

    def test_scrolling_the_panel_s_own_list_does_not_dismiss_it(self):
        """`.capture` is needed and is a trap in the same line.

        A scroll of `.sidebar-nav` moves the trigger and has to close the
        panel — and scroll events do not bubble, so only a capture-phase
        listener on window hears one. But capture hears EVERY scroll in the
        document, including the panel's own list, which is `max-h-96
        overflow-y-auto` in _bell_panel.html and overflows past about five
        notifications. Unguarded, the first scroll gesture inside the panel
        closed it and reset the list to the top, putting everything below the
        fold out of reach.

        The guard also has to survive a target that is not an Element: a
        document-level scroll reports `document` as its target, which has no
        `.closest`, and an unguarded call there throws inside the handler.
        """
        markup = (BASE_HTML.parent / "notifications" / "partials" / "_bell.html").read_text()

        handler = re.search(r'@scroll\.window\.capture="([^"]+)"', markup)

        assert handler, "the panel no longer closes on scroll at all"
        assert "data-bell-panel" in handler.group(1), handler.group(1)
        assert "!$event.target.closest ||" in handler.group(1), handler.group(1)


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

    @pytest.mark.parametrize("element_id", ["nav-badge-notifications", "nav-badge-inbox"])
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
        all read" showed one indicator contradicting another. Two of those
        surfaces are gone for good — the mobile bar names the workspace and
        nothing else, and there is no second bell — so there is one surface,
        and it is the nav row, swapped through the nav's own badge partial
        rather than a convention of its own.

        This count and _badge.html have to change together: a target removed
        from one side and not the other is htmx:oobErrorNoTarget once a minute
        for the whole session, with no test failure and no visible symptom.

        Both counts, because the partial has a branch per count and the zero one
        is the branch that regressed before: it used to drop the target entirely.
        """
        for i in range(unread):
            Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title=f"Ping {i}")

        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'id="nav-badge-notifications"' in body
        assert body.count("hx-swap-oob") == 1


@pytest.mark.django_db
class TestThePolledBadgeAlwaysHasATarget:
    """The 60s poll in _bell.html fires on every authenticated page, and an
    out-of-band swap with no element to land on is an error htmx logs and
    nothing else notices. Both ends of that swap are asserted here: the shell
    renders the target, and the response keeps it.
    """

    def test_the_shell_renders_the_slot_with_nothing_unread(self, shell_body):
        assert shell_body.count('id="nav-badge-notifications"') == 1

    def test_the_slot_shows_no_pill_and_no_zero(self, shell_body):
        """Present in the document, invisible on the screen.

        `sidebar-badge` is the visible pill and lives on the non-zero branch
        only; the zero branch is an empty `hidden` span, which takes no space
        and so cannot open a gap in the row it sits in. Scoped to the slot
        element rather than to the page, since the class legitimately appears
        elsewhere.
        """
        slot = re.search(r'<span id="nav-badge-notifications"[^>]*>(.*?)</span>', shell_body)
        assert slot, "the poll has no target"
        assert slot.group(1) == ""
        assert "sidebar-badge" not in slot.group(0)
        assert "hidden" in slot.group(0)

    def test_reaching_zero_empties_the_slot_rather_than_deleting_it(self, tenancy, client_for):
        """Deleting it was the original bug: an element the response removes is
        one every later poll cannot find, and the count could then never come
        back up in the page either, because every subsequent swap was aiming at
        something that was gone."""
        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'hx-swap-oob="delete"' not in body
        assert 'id="nav-badge-notifications"' in body
        assert "sidebar-badge" not in body

    def test_every_target_the_response_swaps_is_one_the_shell_renders(self, tenancy, client_for):
        """Generalised twice over: across the response's targets, so a fourth
        surface cannot be added to _badge.html without being added to the
        shell, and across the shell's *layouts*, because they are not
        interchangeable — this class exists because a settings page carried the
        bell and none of the nav's badge ids, on 14 pages, missed by sampling
        only the dashboard.

        Unconditional on purpose. A settings page draws the settings rows in
        place of the product's, so for a while it carried neither the poll nor
        the target and this asserted only that the two agreed — which is green
        for a page that has lost both, and so would have passed through a
        regression that dropped the bell everywhere. The row is pinned to the
        settings nav as well now (partials/_app_sidebar.html), so every
        archetype renders the target and the check can be a presence check
        again.
        """
        client = client_for(tenancy.owner)
        response = client.get(reverse("notifications:badge")).content.decode()
        targets = re.findall(r'id="([^"]+)"[^>]*hx-swap-oob', response)
        assert targets

        for url in self.page_archetypes(tenancy):
            shell = client.get(url).content.decode()
            assert reverse("notifications:badge") in shell, f"{url} renders no poll"
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
        """What the sinks used to work around, now true by construction twice.

        A settings page draws the settings rows in place of the product's, so
        the old shape of this was the bug: the nav went, base.html kept
        rendering the poll, and the swap aimed at nothing —
        partials/_nav_badge_sinks.html existed to put hidden copies of the ids
        back. Two things replace it. The poll is a child of the row it updates,
        so it cannot outlive its target; and the row is pinned to the settings
        nav from the same `notifications_row`, so the count is on every page in
        the product rather than on most of them.
        """
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Ping")

        body = client_for(tenancy.owner).get("/accounts/settings/").content.decode()

        assert body.count('id="nav-badge-notifications"') == 1
        assert body.count(reverse("notifications:badge")) == 1
        assert reverse("notifications:bell") in body

    def test_the_settings_nav_does_not_carry_the_bell_twice(self, tenancy, client_for):
        """`notifications_row` IS the row out of `nav_groups`, and the two
        branches in the sidebar are exclusive — but nothing about the markup
        says so, and two copies is the one failure mode this whole class is
        about."""
        body = client_for(tenancy.owner).get("/accounts/settings/").content.decode()
        sidebar = body[body.index("<aside") : body.index("</aside>")]

        assert sidebar.count(">Notifications</span>") == 1
        # ...and the settings rows are still the nav around it, not the
        # product's — this is a pinned utility row, not the nav coming back.
        assert ">Broadcasts</span>" not in sidebar

    @staticmethod
    def page_archetypes(tenancy):
        """One URL per sidebar layout in the product.

        Hand-kept, and the two tests below are what stop that being a liability:
        one holds every layout that takes the sidebar block to rendering it
        through the single shared partial, so a new layout cannot invent an
        <aside> of its own; the other holds the poll to one home. Between them
        a layout missing from this list still cannot strand or duplicate an id.
        """
        return [
            f"/w/{tenancy.workspace.id}/",
            "/accounts/settings/",
            reverse("workspaces:settings", kwargs={"workspace_id": tenancy.workspace.id}),
        ]

    def test_the_poll_is_rendered_only_by_the_row_it_updates(self):
        """The structural half of the pairing above, and the guard that took
        the sinks' place.

        partials/_nav_badge_sinks.html existed because a layout could swap the
        nav out while base.html went on rendering the poll beside it, stranding
        every badge id the swap aims at. A settings layout does swap the nav
        out — that is how settings rows reach the sidebar — so what makes the
        sinks unnecessary is no longer "nothing replaces the nav". It is that
        the poll has exactly one home, inside the Notifications row, and so
        cannot outlive the ids it targets.

        Discovery rather than a hand-kept list: a template that starts the poll
        anywhere else should fail here rather than in a console nobody reads.
        """
        templates = BASE_HTML.parent
        offenders = []
        for path in sorted(templates.rglob("*.html")):
            # Comments stripped first: several templates *describe* the poll in
            # prose, and matching prose would fire on files that render none.
            markup = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", path.read_text(), flags=re.S)
            # Both spellings of the same endpoint, and anywhere in the markup
            # rather than only in an hx-get: a partial that polls it as the
            # hardcoded "/notifications/badge/" copied out of a network tab, or
            # from an htmx.ajax call in a script, strands exactly the same ids
            # as one that reverses the name.
            if re.search(r"notifications:badge|/notifications/badge/", markup):
                offenders.append(str(path.relative_to(templates)))

        assert offenders == ["notifications/partials/_bell.html"], (
            "the 60s badge poll belongs to the Notifications row; started anywhere else it outlives "
            f"the ids it swaps into and needs partials/_nav_badge_sinks.html back: {offenders}"
        )

    def test_every_layout_that_takes_the_sidebar_block_uses_the_one_partial(self):
        """The other half of what `test_no_layout_replaces_the_navigation_any_more`
        used to cover, for a shell where replacing the nav is now legal.

        That test forbade a layout swapping the navigation out at all, which is
        exactly what a settings layout does today — the settings rows reach the
        sidebar through {% templatetag openblock %} block app_sidebar
        {% templatetag closeblock %}. So the rule is no longer "nobody
        replaces it" but "everybody who does goes through the one <aside>":
        a layout that hand-rolled its own would put a second `nav-badge-inbox`
        on every page it serves, and an out-of-band swap only ever finds the
        first.

        Discovery rather than a hand-kept list, because `page_archetypes` above
        IS a hand-kept list and a new layout that never reaches it would
        otherwise be checked by nothing.
        """
        templates = BASE_HTML.parent
        offenders = []
        for path in sorted(templates.rglob("*.html")):
            if path == BASE_HTML:  # where the block is declared, not overridden
                continue
            markup = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", path.read_text(), flags=re.S)
            for override in re.findall(r"{%\s*block app_sidebar\s*%}(.*?){%\s*endblock", markup, flags=re.S):
                if '{% include "partials/_app_sidebar.html"' not in override:
                    offenders.append(str(path.relative_to(templates)))

        assert not offenders, (
            "overrides the sidebar block with markup of its own instead of including "
            f"partials/_app_sidebar.html, which duplicates every badge id in it: {offenders}"
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

    def test_the_style_guide_shows_no_notifications_row(self, client):
        """The row is what would drag the bell onto the page, so it is gated at
        the nav rather than inside the partial — see NavItem.authenticated_only.
        Asserting the label as well as the URL keeps a future row that links
        somewhere else from slipping through."""
        body = client.get("/ui/").content.decode()

        assert ">Notifications</span>" not in body

    def test_the_anonymous_path_runs_no_notification_query(self, client, django_assert_num_queries):
        with django_assert_num_queries(0):
            client.get("/ui/")

    def test_a_request_with_no_user_attribute_does_not_raise(self, rf):
        context = navigation_context(rf.get("/"))

        assert context["unread_notification_count"] == 0


@pytest.mark.django_db
class TestTheBadgeCount:
    def test_it_is_zero_and_renders_nothing_when_nothing_is_unread(self, tenancy, shell_body):
        slot = re.search(r'<span id="nav-badge-notifications"[^>]*>(.*?)</span>', shell_body)

        assert slot, "the poll has no target"
        assert slot.group(1) == ""

    def test_it_reflects_only_this_users_unread_rows(self, tenancy, client_for):
        Notification.objects.create(user=tenancy.members["admin"], event_type="inbox_reminder", title="Theirs")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 0

        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Mine")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 1

    def test_the_count_the_row_renders_is_the_one_the_context_names(self, tenancy):
        """The row and the standalone key read the same number out of the same
        `badges` dict, and this pins that they cannot drift: the bell panel's
        "Mark all read" guard and the notification views use the named key,
        while the row renders from `item["badge"]`."""
        Notification.objects.create(user=tenancy.owner, event_type="inbox_reminder", title="Mine")
        context = navigation_context(_request_for(tenancy.owner, tenancy.workspace))

        assert context["unread_notification_count"] == 1
        row = next(i for g in context["nav_groups"] for i in g["items"] if i["key"] == "notifications")
        assert row["badge"] == 1
        assert row["badge_slot"] is True


def _request_for(user, workspace):
    from django.test import RequestFactory

    request = RequestFactory().get(f"/w/{workspace.id}/")
    request.user = user
    request.workspace = workspace
    request.org_membership = None
    return request
