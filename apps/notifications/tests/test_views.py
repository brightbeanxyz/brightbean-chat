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
