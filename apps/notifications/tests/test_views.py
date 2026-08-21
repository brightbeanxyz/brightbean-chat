"""The bell, the history page and the preference toggle.

The per-user boundary is the security property under test here. It is also
covered by the IDOR sweep now that ``notification_id`` is registered in
``tests/idor.py`` — but that registration is an opt-in, so these tests assert it
directly rather than relying on a sweep that would silently skip the route if
the registration were ever removed.
"""

import pytest
from django.urls import reverse

from apps.notifications.engine import notify
from apps.notifications.models import Notification, NotificationSetting

HTMX = {"HX-Request": "true"}
LOOP_CAP_CONTEXT = {"flow_name": "Welcome", "contact_name": "Ada"}


def make_notification(user, **kwargs):
    return Notification.objects.create(
        user=user,
        event_type=kwargs.pop("event_type", "flow_loop_cap_hit"),
        title=kwargs.pop("title", "Something happened"),
        payload=kwargs.pop("payload", {"tone": "error", "icon": "flows"}),
        **kwargs,
    )


@pytest.mark.django_db
class TestAuthentication:
    @pytest.mark.parametrize(
        ("name", "method"),
        [
            ("notifications:list", "get"),
            ("notifications:bell", "get"),
            ("notifications:badge", "get"),
            ("notifications:mark_all_read", "post"),
            ("notifications:email_preference", "post"),
        ],
    )
    def test_every_route_requires_a_session(self, client, name, method):
        response = getattr(client, method)(reverse(name))

        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]


@pytest.mark.django_db
class TestTheBell:
    def test_it_shows_the_signed_in_users_notifications(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Yours")

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "Yours" in body

    def test_it_never_shows_someone_elses(self, tenancy, client_for):
        make_notification(tenancy.members["admin"], title="Not yours")

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "Not yours" not in body

    def test_it_is_a_partial_not_a_whole_page(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "<html" not in body

    def test_an_empty_feed_says_so(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "All caught up" in body

    def test_a_title_containing_markup_is_escaped(self, tenancy, client_for):
        make_notification(tenancy.owner, title="<script>alert(1)</script>")

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


@pytest.mark.django_db
class TestTheBadge:
    def test_it_shows_a_dot_when_something_is_unread(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert "notif-dot" in body

    def test_it_renders_nothing_when_everything_is_read(self, tenancy, client_for):
        make_notification(tenancy.owner, is_read=True)

        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert "notif-dot" not in body

    def test_it_is_an_out_of_band_swap_so_it_can_ride_any_response(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("notifications:badge")).content.decode()

        assert 'id="notification-badge"' in body
        assert 'hx-swap-oob="true"' in body

    def test_the_shell_renders_the_count_on_an_ordinary_page(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/").content.decode()

        assert "notif-dot" in body
        assert 'href="/notifications/"' in body


@pytest.mark.django_db
class TestMarkRead:
    def test_it_flips_the_row(self, tenancy, client_for):
        notification = make_notification(tenancy.owner)

        client_for(tenancy.owner).post(reverse("notifications:mark_read", args=[notification.id]), headers=HTMX)

        notification.refresh_from_db()
        assert notification.is_read is True
        assert notification.read_at is not None

    def test_the_response_carries_the_refreshed_badge(self, tenancy, client_for):
        notification = make_notification(tenancy.owner)

        body = (
            client_for(tenancy.owner)
            .post(reverse("notifications:mark_read", args=[notification.id]), headers=HTMX)
            .content.decode()
        )

        assert 'id="notification-badge"' in body
        assert "notif-dot" not in body

    def test_another_users_notification_is_a_404_not_a_403(self, tenancy, client_for):
        """A 403 would confirm the id names something real, which over a UUID
        space is the only thing the caller was missing."""
        victim = make_notification(tenancy.owner)

        response = client_for(tenancy.members["admin"]).post(reverse("notifications:mark_read", args=[victim.id]))

        assert response.status_code == 404
        victim.refresh_from_db()
        assert victim.is_read is False

    def test_an_outsiders_attempt_is_also_a_404(self, tenancy, other_tenancy, client_for):
        victim = make_notification(tenancy.owner)

        response = client_for(other_tenancy.owner).post(reverse("notifications:mark_read", args=[victim.id]))

        assert response.status_code == 404

    def test_a_get_is_rejected_on_the_method(self, tenancy, client_for):
        notification = make_notification(tenancy.owner)

        response = client_for(tenancy.owner).get(reverse("notifications:mark_read", args=[notification.id]))

        assert response.status_code == 405

    def test_mark_all_read_clears_everything_of_this_users(self, tenancy, client_for):
        make_notification(tenancy.owner)
        make_notification(tenancy.owner)
        someone_else = make_notification(tenancy.members["admin"])

        client_for(tenancy.owner).post(reverse("notifications:mark_all_read"), headers=HTMX)

        assert not Notification.objects.filter(user=tenancy.owner, is_read=False).exists()
        someone_else.refresh_from_db()
        assert someone_else.is_read is False


@pytest.mark.django_db
class TestTheHistoryPage:
    def test_it_lists_this_users_notifications(self, tenancy, client_for):
        notify(tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context=LOOP_CAP_CONTEXT)

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert "hit the loop cap" in body

    def test_it_returns_a_partial_to_htmx_and_a_page_otherwise(self, tenancy, client_for):
        client = client_for(tenancy.owner)

        assert "<html" in client.get(reverse("notifications:list")).content.decode()
        assert "<html" not in client.get(reverse("notifications:list"), headers=HTMX).content.decode()

    def test_the_type_filter_narrows_the_list(self, tenancy, client_for):
        make_notification(tenancy.owner, event_type="flow_loop_cap_hit", title="Loop")
        make_notification(tenancy.owner, event_type="inbox_reminder", title="Remind")

        body = (
            client_for(tenancy.owner)
            .get(reverse("notifications:list"), {"event_type": "inbox_reminder"})
            .content.decode()
        )

        assert "Remind" in body
        assert "Loop" not in body

    def test_an_unregistered_filter_value_is_ignored_rather_than_erroring(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Loop")

        body = (
            client_for(tenancy.owner)
            .get(reverse("notifications:list"), {"event_type": "'; DROP TABLE"})
            .content.decode()
        )

        assert "Loop" in body

    def test_the_read_state_filter_works_both_ways(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Fresh")
        make_notification(tenancy.owner, title="Seen", is_read=True)
        client = client_for(tenancy.owner)

        unread = client.get(reverse("notifications:list"), {"read_state": "unread"}).content.decode()
        read = client.get(reverse("notifications:list"), {"read_state": "read"}).content.decode()

        assert "Fresh" in unread and "Seen" not in unread
        assert "Seen" in read and "Fresh" not in read

    def test_paging_does_not_cost_a_second_count_query(self, tenancy, client_for):
        for index in range(35):
            make_notification(tenancy.owner, title=f"Row {index}")

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert "Older" in body
        assert body.count("notif-item") == 30

    def test_a_junk_page_number_falls_back_to_the_first(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Only row")

        body = client_for(tenancy.owner).get(reverse("notifications:list"), {"page": "not-a-number"}).content.decode()

        assert "Only row" in body


@pytest.mark.django_db
class TestTheEmailToggle:
    def test_it_defaults_to_on_without_writing_a_row(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert 'name="email_enabled"' in body
        assert "checked" in body
        assert not NotificationSetting.objects.exists()

    def test_turning_it_off_persists(self, tenancy, client_for):
        client_for(tenancy.owner).post(reverse("notifications:email_preference"), {}, headers=HTMX)

        assert NotificationSetting.objects.get(user=tenancy.owner).email_enabled is False

    def test_turning_it_back_on_persists(self, tenancy, client_for):
        NotificationSetting.objects.create(user=tenancy.owner, email_enabled=False)

        client_for(tenancy.owner).post(reverse("notifications:email_preference"), {"email_enabled": "on"}, headers=HTMX)

        assert NotificationSetting.objects.get(user=tenancy.owner).email_enabled is True

    def test_it_confirms_with_a_toast(self, tenancy, client_for):
        response = client_for(tenancy.owner).post(reverse("notifications:email_preference"), {}, headers=HTMX)

        assert response.status_code == 204
        assert "showToast" in response["HX-Trigger"]

    def test_one_persons_preference_is_not_anothers(self, tenancy, client_for):
        client_for(tenancy.owner).post(reverse("notifications:email_preference"), {}, headers=HTMX)

        assert not NotificationSetting.objects.filter(user=tenancy.members["admin"]).exists()


@pytest.mark.django_db
class TestWriteViewsDoNotRenderASurface:
    """Two surfaces show these rows. An earlier cut had the write views return
    the bell partial to whoever asked, so marking a row read from the history
    page swapped the dropdown into the list container and lost the reader's
    filter and page. The write views now return only the badge plus an event.
    """

    def test_mark_read_does_not_return_the_bell_panel(self, tenancy, client_for):
        notification = make_notification(tenancy.owner)

        body = (
            client_for(tenancy.owner)
            .post(reverse("notifications:mark_read", args=[notification.id]), headers=HTMX)
            .content.decode()
        )

        # Markup unique to the dropdown: its footer link and its own heading.
        assert "View all notifications" not in body
        assert "All caught up" not in body

    def test_mark_all_read_does_not_return_the_bell_panel(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).post(reverse("notifications:mark_all_read"), headers=HTMX).content.decode()

        assert "View all notifications" not in body
        assert "All caught up" not in body

    @pytest.mark.parametrize("route", ["notifications:mark_all_read", None])
    def test_both_write_views_fire_the_refresh_event(self, tenancy, client_for, route):
        notification = make_notification(tenancy.owner)
        url = reverse(route) if route else reverse("notifications:mark_read", args=[notification.id])

        response = client_for(tenancy.owner).post(url, headers=HTMX)

        assert "notificationsChanged" in response["HX-Trigger"]

    def test_the_response_is_the_badge_and_nothing_else(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).post(reverse("notifications:mark_all_read"), headers=HTMX).content.decode()

        assert 'id="notification-badge"' in body
        assert "notif-dot" not in body

    def test_marking_read_from_the_history_page_leaves_the_list_intact(self, tenancy, client_for):
        """The concrete regression: the history page posts these too."""
        notification = make_notification(tenancy.owner, title="Still here")
        client = client_for(tenancy.owner)

        client.post(reverse("notifications:mark_read", args=[notification.id]), headers=HTMX)
        body = client.get(reverse("notifications:list"), headers=HTMX).content.decode()

        assert "Still here" in body
        assert "notif-item" in body


@pytest.mark.django_db
class TestTheHistoryPageCarriesNoResponseOnlyMarkup:
    """_history_list.html is rendered both as an htmx response and inline by
    list.html, so anything in it that only makes sense in a response — an
    out-of-band badge carrying ids the shell already renders — is a duplicate
    id on the full page."""

    @pytest.mark.parametrize("element_id", ["notification-badge", "nav-badge-notifications"])
    def test_the_full_page_carries_each_badge_id_at_most_once(self, tenancy, client_for, element_id):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert body.count(f'id="{element_id}"') <= 1

    def test_the_full_page_carries_no_out_of_band_attributes(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert "hx-swap-oob" not in body

    def test_the_container_refreshes_itself_with_its_filters(self, tenancy, client_for):
        """The state the write view deliberately does not try to rebuild."""
        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert 'hx-trigger="notificationsChanged from:body"' in body
        # Named individually rather than as one literal, so extending the
        # include list does not fail a test about the trigger wiring.
        for field in ("event_type", "read_state", "page"):
            assert f"[name='{field}']" in body


@pytest.mark.django_db
class TestPagingIsBounded:
    def test_an_enormous_page_number_is_clamped_not_a_500(self, tenancy, client_for):
        """Python ints are arbitrary precision, so this parses and then
        overflows the bigint Postgres wants for OFFSET."""
        response = client_for(tenancy.owner).get(reverse("notifications:list"), {"page": "99999999999999999999"})

        assert response.status_code == 200

    def test_a_negative_page_falls_back_to_the_first(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Only row")

        body = client_for(tenancy.owner).get(reverse("notifications:list"), {"page": "-5"}).content.decode()

        assert "Only row" in body

    def test_paging_is_stable_across_a_single_fan_out(self, tenancy, client_for):
        """Every row in one notify() call shares a created_at to the
        microsecond, so without a unique tiebreak the two pages can overlap."""
        for index in range(40):
            make_notification(tenancy.owner, title=f"Row {index:02d}")
        client = client_for(tenancy.owner)

        first = client.get(reverse("notifications:list"), {"page": 1}, headers=HTMX).content.decode()
        second = client.get(reverse("notifications:list"), {"page": 2}, headers=HTMX).content.decode()

        on_first = {f"Row {i:02d}" for i in range(40) if f"Row {i:02d}" in first}
        on_second = {f"Row {i:02d}" for i in range(40) if f"Row {i:02d}" in second}
        assert not on_first & on_second, "a row appeared on both pages"
        assert len(on_first | on_second) == 40, "a row appeared on neither page"

    def test_the_pager_url_encodes_the_filter_values(self, tenancy, client_for):
        for index in range(35):
            make_notification(tenancy.owner, title=f"Row {index}")

        body = (
            client_for(tenancy.owner)
            .get(reverse("notifications:list"), {"event_type": "a&read_state=read"})
            .content.decode()
        )

        assert "a%26read_state%3Dread" in body


@pytest.mark.django_db
class TestMarkAllReadKeepsUpdatedAtHonest:
    def test_the_bulk_path_moves_updated_at_like_the_single_path(self, tenancy, client_for):
        """update() bypasses auto_now, so without an explicit value the two
        paths would leave the column meaning two different things."""
        notification = make_notification(tenancy.owner)
        before = notification.updated_at

        client_for(tenancy.owner).post(reverse("notifications:mark_all_read"), headers=HTMX)

        notification.refresh_from_db()
        assert notification.updated_at > before


@pytest.mark.django_db
class TestClickingANotificationGoesWhereItPoints:
    """hx-post cancels an anchor's own navigation, so without an explicit
    redirect the reader clicks a notification, watches it turn read, and has to
    click again to reach its target."""

    def test_following_a_row_redirects_to_its_action_url(self, tenancy, client_for):
        notification = make_notification(
            tenancy.owner, payload={"action_url": "/w/abc/flows/1", "tone": "error", "icon": "flows"}
        )

        response = client_for(tenancy.owner).post(
            reverse("notifications:mark_read", args=[notification.id]), {"follow": "1"}, headers=HTMX
        )

        assert response["HX-Redirect"] == "/w/abc/flows/1"
        notification.refresh_from_db()
        assert notification.is_read is True

    def test_the_history_pages_mark_read_button_stays_put(self, tenancy, client_for):
        """It posts the same route and must not navigate anyone anywhere."""
        notification = make_notification(tenancy.owner, payload={"action_url": "/w/abc/flows/1"})

        response = client_for(tenancy.owner).post(
            reverse("notifications:mark_read", args=[notification.id]), headers=HTMX
        )

        assert "HX-Redirect" not in response

    def test_a_row_with_no_target_does_not_redirect(self, tenancy, client_for):
        notification = make_notification(tenancy.owner, payload={"tone": "info"})

        response = client_for(tenancy.owner).post(
            reverse("notifications:mark_read", args=[notification.id]), {"follow": "1"}, headers=HTMX
        )

        assert "HX-Redirect" not in response

    def test_a_stored_off_site_url_is_still_refused_at_redirect_time(self, tenancy, client_for):
        """A redirect built from a stored value is exactly where a row written
        before the allowlist existed would matter."""
        notification = make_notification(tenancy.owner)
        Notification.objects.filter(pk=notification.pk).update(payload={"action_url": "https://evil.test/login"})

        response = client_for(tenancy.owner).post(
            reverse("notifications:mark_read", args=[notification.id]), {"follow": "1"}, headers=HTMX
        )

        assert "HX-Redirect" not in response

    def test_the_bell_row_asks_to_be_followed(self, tenancy, client_for):
        make_notification(tenancy.owner, payload={"action_url": "/w/abc/flows/1"})

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "follow" in body


@pytest.mark.django_db
class TestTheHistoryPageIsActionable:
    def test_a_row_with_a_target_links_to_it(self, tenancy, client_for):
        """Otherwise a notification that scrolls out of the bell's 20-row
        window is reachable from the email but from nowhere in the product."""
        make_notification(tenancy.owner, title="Go here", payload={"action_url": "/w/abc/flows/1"})

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert 'href="/w/abc/flows/1"' in body

    def test_a_row_without_one_stays_plain_text(self, tenancy, client_for):
        make_notification(tenancy.owner, title="Nowhere to go", payload={"tone": "info"})

        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert "Nowhere to go" in body
        assert 'href=""' not in body


@pytest.mark.django_db
class TestTheRefreshKeepsTheReadersPlace:
    def test_the_page_number_travels_with_the_refresh(self, tenancy, client_for):
        for index in range(70):
            make_notification(tenancy.owner, title=f"Row {index:02d}")

        body = client_for(tenancy.owner).get(reverse("notifications:list"), {"page": 2}, headers=HTMX).content.decode()

        assert '<input type="hidden" name="page" value="2">' in body

    def test_the_container_includes_the_page_input(self, tenancy, client_for):
        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()

        assert "[name='page']" in body

    def test_changing_a_filter_does_not_carry_the_page(self, tenancy, client_for):
        """A new filter is a different result set, so page 1 is right."""
        body = client_for(tenancy.owner).get(reverse("notifications:list")).content.decode()
        selects = body[body.index('name="event_type"') : body.index("notification-history")]

        assert "[name='page']" not in selects


@pytest.mark.django_db
class TestTheMobileIndicator:
    def test_the_shell_gives_it_its_own_swap_target(self, tenancy, client_for):
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.id}/").content.decode()

        assert body.count('id="notification-badge-mobile"') == 1

    def test_marking_read_clears_it_too(self, tenancy, client_for):
        """It sits outside both desktop targets, so it would otherwise keep
        showing a dot after the last notification was read."""
        make_notification(tenancy.owner)

        body = client_for(tenancy.owner).post(reverse("notifications:mark_all_read"), headers=HTMX).content.decode()

        assert 'id="notification-badge-mobile"' in body
        assert body.count("hx-swap-oob") == 3
        assert "notif-dot" not in body
